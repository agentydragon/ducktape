package dispatcher

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
)

func testPlacement(spec *sessionpb.SessionSpec, sandboxName, sandboxUID string) placement.SessionPlacement {
	if placement.PlacementOf(spec) == "" {
		spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}
	}
	return placement.PlacementOf(spec)
}

func createSession(s *store.Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string) (*store.Session, error) {
	return createSessionForCaller(s, ctx, sessionID, sandboxName, sandboxUID, spec, setupScript, testExecutionIdentity(), testStoreCaller())
}

func testExecutionIdentity() *identitypb.Identity {
	return &identitypb.Identity{Name: "dispatcher-test-identity", Uid: "00000000-0000-4000-8000-00000000a000"}
}

// testStoreCaller is the privileged component caller store fixtures write as.
func testStoreCaller() store.ServiceCaller { return store.ServiceCaller{SPIFFEID: peertls.AppURI} }

func createSessionForCaller(s *store.Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, executionIdentity *identitypb.Identity, caller store.Caller) (*store.Session, error) {
	return s.CreateSessionWithAdmissionCheck(ctx, sessionID, sandboxName, sandboxUID, testPlacement(spec, sandboxName, sandboxUID),
		spec, setupScript, store.SessionExecution{Identity: executionIdentity}, caller, nil)
}

func expireSessionLease(t *testing.T, queueStore *store.Store, owner store.SessionOwner) {
	t.Helper()
	if _, err := queueStore.DB.ExecContext(t.Context(), `UPDATE session_delivery_lease
		SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=$1 AND holder=$2`, owner.SessionID, owner.Holder); err != nil {
		t.Fatal(err)
	}
}
