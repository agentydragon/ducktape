package main

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/google/uuid"
	mcpauth "github.com/modelcontextprotocol/go-sdk/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	testConnectionID = "018f4e32-6632-7e7f-83dd-7f90ccf84f68"
	testGrantID      = "018f4e32-6632-7e7f-83dd-7f90ccf84f69"
	projectedToken   = "projected-action-audience-token"
)

var (
	testIdentity = auth.Identity{Name: "mcp-subject", UID: "uid-mcp-subject"}
	testGrant    = connections.ResolvedGrant{
		ConnectionID: uuid.MustParse(testConnectionID), GrantID: uuid.MustParse(testGrantID), Identity: testIdentity,
	}
)

type watchActionServer struct {
	pb.UnimplementedActionServiceServer
	started chan metadata.MD
}

func (s *watchActionServer) WatchActionRequest(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.started <- md
	if err := stream.Send(&pb.WatchActionRequestResponse{}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func startActionServer(t *testing.T) (string, <-chan metadata.MD) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	started := make(chan metadata.MD, 1)
	pb.RegisterActionServiceServer(server, &watchActionServer{started: started})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	t.Cleanup(func() { _ = listener.Close() })
	return listener.Addr().String(), started
}

func TestConnectionDelegationUsesProjectedActorTokenAndTypedProvenance(t *testing.T) {
	target, started := startActionServer(t)
	tokenPath := filepath.Join(t.TempDir(), "action-token")
	if err := os.WriteFile(tokenPath, []byte(projectedToken), 0o600); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	revoked := false
	caller := &coreCaller{
		authority: func(_ context.Context, bearer string) (*connections.ResolvedGrant, error) {
			mu.Lock()
			defer mu.Unlock()
			if bearer != "external-connection-token" || revoked {
				return nil, mcpauth.ErrInvalidToken
			}
			grant := testGrant
			return &grant, nil
		},
		recheckPeriod: 10 * time.Millisecond,
	}
	clientConn, err := dialActions(target, tokenPath, time.Second, nil, caller)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = clientConn.Close() })

	ctx := context.WithValue(t.Context(), edgeBearerKey{}, "external-connection-token")
	watch, err := pb.NewActionServiceClient(clientConn).WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatalf("first Action frame: %v", err)
	}
	md := <-started
	if got := md.Get("authorization"); len(got) != 1 || got[0] != auth.DelegatedScheme+" "+projectedToken {
		t.Fatalf("Action actor credentials %q", got)
	}
	delegations := md.Get(auth.DelegationMetadataKey)
	if len(delegations) != 1 {
		t.Fatalf("delegation metadata values %d, want one", len(delegations))
	}
	envelope := new(delegationpb.Delegation)
	if err := proto.Unmarshal([]byte(delegations[0]), envelope); err != nil {
		t.Fatal(err)
	}
	if got := envelope.GetIdentity(); got == nil || got.GetName() != testIdentity.Name || got.GetUid() != testIdentity.UID {
		t.Fatalf("effective Identity %v", got)
	}
	provenance := envelope.GetConnection()
	if provenance == nil || provenance.GetConnectionId() != testConnectionID || provenance.GetGrantId() != testGrantID {
		t.Fatalf("Connection provenance %v", provenance)
	}

	mu.Lock()
	revoked = true
	mu.Unlock()
	if _, err := watch.Recv(); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("revoked Connection left its Action stream open: %v", err)
	}
}

func TestConnectionDelegationRefusesInvalidBearerWithoutActorFallback(t *testing.T) {
	target, started := startActionServer(t)
	tokenPath := filepath.Join(t.TempDir(), "action-token")
	if err := os.WriteFile(tokenPath, []byte(projectedToken), 0o600); err != nil {
		t.Fatal(err)
	}
	caller := &coreCaller{authority: func(context.Context, string) (*connections.ResolvedGrant, error) {
		return nil, errors.Join(mcpauth.ErrInvalidToken, errors.New("revoked grant"))
	}}
	clientConn, err := dialActions(target, tokenPath, time.Second, nil, caller)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = clientConn.Close() })
	ctx := context.WithValue(t.Context(), edgeBearerKey{}, "revoked-token")
	_, err = pb.NewActionServiceClient(clientConn).WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("invalid Connection bearer: %v", err)
	}
	select {
	case <-started:
		t.Fatal("Action received a call after the Connection bearer was refused")
	default:
	}
}
