package main

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	bbspb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type largeInvocationServer struct {
	bbspb.UnimplementedBuildBuddyServiceServer
	key string
}

func (s *largeInvocationServer) GetInvocation(ctx context.Context, req *invocationpb.GetInvocationRequest) (*invocationpb.GetInvocationResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if keys := md.Get("x-buildbuddy-api-key"); len(keys) != 1 || keys[0] != s.key {
		return nil, status.Error(codes.Unauthenticated, "missing API key")
	}
	return &invocationpb.GetInvocationResponse{Invocation: []*invocationpb.Invocation{{
		InvocationId: req.GetLookup().GetInvocationId(), ConsoleBuffer: strings.Repeat("x", 5<<20),
	}}}, nil
}

func TestGRPCInvocationAcceptsLargeAuthenticatedResponse(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	bbspb.RegisterBuildBuddyServiceServer(server, &largeInvocationServer{key: "test-key"})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := newRPCConnection("http://"+listener.Addr().String(), "test-key", 5*time.Second, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	response, err := bbspb.NewBuildBuddyServiceClient(conn).GetInvocation(context.Background(), &invocationpb.GetInvocationRequest{Lookup: &invocationpb.InvocationLookup{InvocationId: "inv-id"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(response.GetInvocation()) != 1 || response.GetInvocation()[0].GetInvocationId() != "inv-id" || len(response.GetInvocation()[0].GetConsoleBuffer()) != 5<<20 {
		t.Fatal("large invocation response was truncated")
	}
}

func TestGRPCReadRetriesRespectDeadlineAndCredentials(t *testing.T) {
	calls := 0
	interceptor := readRPCInterceptor("test-key", 20*time.Millisecond, 3)
	err := interceptor(context.Background(), "/buildbuddy.service.BuildBuddyService/GetInvocation", nil, nil, nil,
		func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
			calls++
			md, _ := metadata.FromOutgoingContext(ctx)
			if values := md.Get("x-buildbuddy-api-key"); len(values) != 1 || values[0] != "test-key" {
				t.Fatalf("metadata = %v", md)
			}
			if _, ok := ctx.Deadline(); !ok {
				t.Error("missing deadline")
			}
			return status.Error(codes.Unavailable, "temporary failure")
		})
	if status.Code(err) != codes.DeadlineExceeded || calls != 1 {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
}

func TestGRPCDoesNotRetryPermanentEventLogIndexLimit(t *testing.T) {
	calls := 0
	err := readRPCInterceptor("test-key", time.Second, 3)(context.Background(), "/buildbuddy.service.BuildBuddyService/GetEventLogChunk", nil, nil, nil,
		func(context.Context, string, any, any, *grpc.ClientConn, ...grpc.CallOption) error {
			calls++
			return status.Error(codes.ResourceExhausted, eventLogIndexLimitExceededMessage)
		})
	if calls != 1 || status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("calls=%d err=%v, want one permanent cursor failure", calls, err)
	}
}

func TestGRPCDoesNotRetryPermissionErrorsOrMutations(t *testing.T) {
	for _, test := range []struct {
		method string
		code   codes.Code
	}{
		{"/buildbuddy.service.BuildBuddyService/GetInvocation", codes.PermissionDenied},
		{"/buildbuddy.service.BuildBuddyService/CancelExecutions", codes.Unavailable},
	} {
		calls := 0
		err := readRPCInterceptor("test-key", time.Second, 3)(context.Background(), test.method, nil, nil, nil,
			func(context.Context, string, any, any, *grpc.ClientConn, ...grpc.CallOption) error {
				calls++
				return status.Error(test.code, "failed")
			})
		if calls != 1 || status.Code(err) != test.code {
			t.Fatalf("method=%s calls=%d err=%v", test.method, calls, err)
		}
	}
}
