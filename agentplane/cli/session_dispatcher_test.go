package cli

import (
	"context"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestCreateSessionRequiresCanonicalUUIDBeforeConnecting(t *testing.T) {
	for _, sessionID := range []string{"not-a-uuid", "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF", "abcdefabcdef4abcdefabcdefabcdef"} {
		code, stdout, stderr := invoke(nil, "session-dispatcher", "create", sessionID, "--sandbox", "sandbox", "--sandbox-uid", "uid", "--spec", "{}")
		if code != invocation.ExitUsage || stdout != "" || !strings.Contains(stderr, "SESSION_ID must be a canonical UUID") {
			t.Errorf("session-dispatcher create %q: exit %d, stdout %q, stderr %q; want canonical UUID usage error", sessionID, code, stdout, stderr)
		}
	}
}

func TestSessionListOmitsAuthorizationMetadataWithoutBearerConfiguration(t *testing.T) {
	server := grpc.NewServer()
	fixture := &sessionListFixture{
		requests:       make(chan *dispatcherpb.ListSessionsRequest, 1),
		authorizations: make(chan []string, 2),
	}
	dispatcherpb.RegisterSessionDispatcherServer(server, fixture)
	address := grpcserver.Serve(t, server)
	env := map[string]string{
		"AGENTPLANE_SESSIONS_URL": "http://" + address,
	}
	for _, asJSON := range []bool{true, false} {
		args := []string{"session-dispatcher", "list", "--limit", "1", "--after", "previous-page"}
		if asJSON {
			args = append([]string{"--json"}, args...)
		}
		code, stdout, stderr := invoke(env, args...)
		if code != 0 {
			t.Fatalf("session-dispatcher list: exit %d, stdout %q, stderr %q", code, stdout, stderr)
		}
		request := <-fixture.requests
		if authorization := <-fixture.authorizations; len(authorization) != 0 {
			t.Fatalf("Authorization metadata = %v; want no bearer when Sessions credential is unset", authorization)
		}
		if request.GetLimit() != 1 || request.GetPageToken() != "previous-page" {
			t.Fatalf("list request: %v", request)
		}
		if asJSON {
			page := &dispatcherpb.ListSessionsResponse{}
			if err := protojson.Unmarshal([]byte(stdout), page); err != nil {
				t.Fatal(err)
			}
			if len(page.GetSessions()) != 1 || page.GetSessions()[0].GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING || page.GetNextPageToken() != "next-page" || stderr != "" {
				t.Fatalf("list JSON page: %v, stderr %q", page, stderr)
			}
		} else if !strings.Contains(stdout, "58cae82c-453d-4de7-ae5b-17a8f7596077") || !strings.Contains(stdout, "waiting") || !strings.Contains(stderr, "--after next-page") {
			t.Fatalf("list text: stdout %q, stderr %q", stdout, stderr)
		}
	}
}

type sessionListFixture struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	requests       chan *dispatcherpb.ListSessionsRequest
	authorizations chan []string
}

func (f *sessionListFixture) ListSessions(ctx context.Context, request *dispatcherpb.ListSessionsRequest) (*dispatcherpb.ListSessionsResponse, error) {
	f.requests <- request
	authorization, _ := metadata.FromIncomingContext(ctx)
	f.authorizations <- authorization.Get("authorization")
	return &dispatcherpb.ListSessionsResponse{
		Sessions: []*dispatcherpb.Session{{
			SessionId: "58cae82c-453d-4de7-ae5b-17a8f7596077", Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "queued-sandbox", Uid: "queued-sandbox-uid"}}}},
			Status: &dispatcherpb.SessionStatus{Materialization: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING},
		}},
		NextPageToken: "next-page",
	}, nil
}
