package dispatcher

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placementtest"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
)

func createSession(s *store.Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *auth.Account) (*store.Session, error) {
	placementtest.EnsurePlacement(spec, sandboxName, sandboxUID)
	return s.CreateSessionWithPlacementForCaller(ctx, sessionID, sandboxName, sandboxUID, spec, setupScript, wakeCaller,
		store.SessionExecution{Identity: testExecutionIdentity()}, testStoreCaller(wakeCaller))
}

func testExecutionIdentity() *identitypb.Identity {
	return &identitypb.Identity{Name: "dispatcher-test-identity", Uid: "00000000-0000-4000-8000-00000000a000"}
}

func testStoreCaller(wakeCaller *auth.Account) store.ServiceAccountCaller {
	actor := serviceAccountIdentityFor(auth.Account{Namespace: "dispatcher-test", Name: "store-fixture"}, "dispatcher-test-actor-uid")
	effective := actor
	if wakeCaller != nil {
		effective = serviceAccountIdentityFor(*wakeCaller, "dispatcher-test-effective-uid")
	}
	attribution := &store.AdmissionAttribution{Actor: actor, EffectiveCaller: effective}
	return store.ServiceAccountCaller{ServiceAccountIdentity: effective, Privileged: true, Attribution: attribution}
}

func createSessionForCaller(s *store.Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *auth.Account, executionIdentity *identitypb.Identity, caller store.ServiceAccountCaller) (*store.Session, error) {
	placementtest.EnsurePlacement(spec, sandboxName, sandboxUID)
	return s.CreateSessionWithPlacementForCaller(ctx, sessionID, sandboxName, sandboxUID, spec, setupScript, wakeCaller, store.SessionExecution{Identity: executionIdentity}, caller)
}

func expireSessionLease(t *testing.T, queueStore *store.Store, owner store.SessionOwner) {
	t.Helper()
	if _, err := queueStore.DB.ExecContext(t.Context(), `UPDATE session_delivery_lease
		SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=$1 AND holder=$2`, owner.SessionID, owner.Holder); err != nil {
		t.Fatal(err)
	}
}
