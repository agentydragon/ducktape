package gateway

import (
	"context"
	"net"
	"strings"
	"testing"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/emptypb"
)

func TestGRPCManagementRequiresOwnerBearer(t *testing.T) {
	service, err := New(nil)
	if err != nil {
		t.Fatal(err)
	}
	token := strings.Repeat("o", 40)
	server := GRPCServer(service, token)
	listener := bufconn.Listen(1024 * 1024)
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()
	conn, err := grpc.NewClient("passthrough:///synthetic", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	client := managementpb.NewSessionManagementClient(conn)
	_, err = client.ListInstances(context.Background(), &emptypb.Empty{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("missing token: %v", err)
	}
	ctx := metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+token))
	if _, err = client.ListInstances(ctx, &emptypb.Empty{}); err != nil {
		t.Fatalf("valid token: %v", err)
	}
	ctx = metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+token, "authorization", "Bearer "+token))
	_, err = client.ListInstances(ctx, &emptypb.Empty{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("duplicate token: %v", err)
	}
}
