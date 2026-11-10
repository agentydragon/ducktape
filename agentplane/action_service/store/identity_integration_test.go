package store

import (
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

var (
	identityCaller = IdentityPrincipal{Identity: Identity{Name: "test-identity", UID: "test-identity-uid"}}
	// successor is a later Identity of the same name: another incarnation, which owns nothing of the first's.
	successor = IdentityPrincipal{Identity: Identity{Name: "test-identity", UID: "test-successor-uid"}}
)

func TestAnIdentityOwnsItsRequestsByIncarnation(t *testing.T) {
	f := newFixture(t)
	request := f.submit(t, identityCaller, "identity-owned")
	if request.Caller != (IdentityCaller{Identity: identityCaller.Identity}) {
		t.Errorf("caller = %+v", request.Caller)
	}

	for name, principal := range map[string]ReadPrincipal{"owner": identityCaller, "operator": operator, "service reader": reader} {
		if got, err := f.Get(t.Context(), request.ID, principal); err != nil || got.ID != request.ID {
			t.Errorf("%s: Get = %v, %v", name, got.ID, err)
		}
	}
	for name, principal := range map[string]Owner{"a successor": successor, "a ServiceAccount": caller} {
		if _, err := f.Get(t.Context(), request.ID, principal); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: Get = %v", name, err)
		}
		if got := f.list(t, principal, ListFilter{}); len(got) != 0 {
			t.Errorf("%s lists %v", name, ids(got))
		}
		if _, err := f.Cancel(t.Context(), request.ID, principal); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: Cancel = %v", name, err)
		}
	}
	if got := f.list(t, identityCaller, ListFilter{}); len(got) != 1 || got[0].ID != request.ID {
		t.Errorf("the owner lists %v", ids(got))
	}

	// The key is the incarnation's.
	if _, err := f.Submit(t.Context(), identityCaller, input("identity-owned"), Admission{RequestID: uuid.New()}); conflictReason(err) != ConflictIdempotencyKeyUsed {
		t.Errorf("resubmitting the key: %v", err)
	}
	for name, owner := range map[string]Owner{"a successor": successor, "a ServiceAccount": caller} {
		if other := f.submit(t, owner, "identity-owned"); other.ID == request.ID {
			t.Errorf("%s reached the Identity's request", name)
		}
	}

	result, err := f.Cancel(t.Context(), request.ID, identityCaller)
	if err != nil || result.Outcome != OutcomeCancelled {
		t.Fatalf("Cancel = %+v, %v", result, err)
	}
	events := f.events(t, request.ID)
	wantActor := &action_servicepb.Principal{Principal: &action_servicepb.Principal_CallerIdentity{CallerIdentity: identityCaller.Identity.Proto()}}
	if len(events) != 2 || !proto.Equal(events[1].Actor, wantActor) {
		t.Errorf("events %+v", events)
	}
}

func TestAnIdentitysExecutionCarriesItsIdentity(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, identityCaller, "identity-run")
	f.claim(t, allowed.ID)
	if payload := f.markRunning(t, allowed.ID); payload.Caller != (IdentityCaller{Identity: identityCaller.Identity}) {
		t.Errorf("payload caller = %+v", payload.Caller)
	}
}

// A newer release may record a kind of caller this one does not know, after its migration widens the constraint.
func TestARequestOfAnUnknownKindOfCallerIsNoCallersOwn(t *testing.T) {
	f := newFixture(t)
	f.exec(t, `ALTER TABLE action_request DROP CONSTRAINT action_request_one_caller`)
	id := uuid.New()
	f.exec(t, `INSERT INTO action_request (id, idempotency_key, action_group, action_name, arguments, title, state, version,
		created_at, updated_at) VALUES ($1, 'unknown-caller', 'test-group', 'test-action', '{}', 'test title', 'decision_pending', 1,
		now(), now())`, id)

	if got := f.get(t, id); got.Caller != (UnknownCaller{}) {
		t.Errorf("caller = %+v", got.Caller)
	}
	for name, principal := range map[string]Owner{"a ServiceAccount": caller, "an Identity": identityCaller} {
		if _, err := f.Get(t.Context(), id, principal); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: Get = %v", name, err)
		}
	}
}
