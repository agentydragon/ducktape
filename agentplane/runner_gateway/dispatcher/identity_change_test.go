package dispatcher

import (
	"context"
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func seedIdentitySession(t *testing.T, store *Store, sandboxUID string, identity *identitypb.Identity, check func(context.Context) error) string {
	t.Helper()
	id := uuid.NewString()
	spec := &sessionpb.SessionSpec{}
	_, err := store.createSessionWithPlacementForCaller(t.Context(), id, "test-sandbox", sandboxUID,
		testPlacement(spec, "test-sandbox", sandboxUID), spec, nil, nil,
		SessionExecution{Identity: identity},
		IdentityCaller{Identity: identity, Proof: IdentitySVID{}}, check)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func TestLiveIdentityRecheckDoesNotRejectExistingSessionReceipt(t *testing.T) {
	store := testStore(t, 10)
	sandboxUID := uuid.NewString()
	identity := &identitypb.Identity{Name: "test-agent", Uid: uuid.NewString()}
	id := seedIdentitySession(t, store, sandboxUID, identity, nil)
	spec := &sessionpb.SessionSpec{}
	checked := false
	_, err := store.createSessionWithPlacementForCaller(t.Context(), id, "test-sandbox", sandboxUID,
		testPlacement(spec, "test-sandbox", sandboxUID), spec, nil, nil,
		SessionExecution{Identity: identity},
		IdentityCaller{Identity: identity, Proof: IdentitySVID{}},
		func(context.Context) error { checked = true; return status.Error(codes.FailedPrecondition, "changed") })
	if err != nil || checked {
		t.Fatalf("existing receipt = %v, check called=%t", err, checked)
	}
}
