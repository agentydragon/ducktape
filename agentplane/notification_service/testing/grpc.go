package testing

import (
	"context"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/metadata"
)

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
