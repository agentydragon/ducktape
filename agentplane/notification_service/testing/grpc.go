package testing

import (
	"context"
	"net"
	"testing"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// ServeGRPC serves a fake peer on a loopback port and returns its address.
func ServeGRPC(t testing.TB, register func(*grpc.Server)) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	register(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	return listener.Addr().String()
}

// ListingRunner is a runner peer whose sessions, error, and received metadata are controlled by a test.
type ListingRunner struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	Sessions []string
	Err      error
	Received chan metadata.MD
}

func (r *ListingRunner) ListSessions(ctx context.Context, _ *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	if r.Received != nil {
		incoming, _ := metadata.FromIncomingContext(ctx)
		r.Received <- incoming
	}
	if r.Err != nil {
		return nil, r.Err
	}
	response := &sessionpb.ListSessionsResponse{}
	for _, id := range r.Sessions {
		response.Sessions = append(response.Sessions, &sessionpb.SessionSummary{SessionId: id})
	}
	return response, nil
}
