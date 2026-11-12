package dispatcher

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func waitForAdvisoryWaiter(t *testing.T, store *Store) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		var waiting bool
		err := store.DB.QueryRowContext(t.Context(), `SELECT EXISTS (
			SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted
			AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))`).Scan(&waiting)
		if err != nil {
			t.Fatal(err)
		}
		if waiting {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("no transaction waited on the Sandbox advisory lock")
}

func seedIdentitySession(t *testing.T, store *Store, sandboxUID string, identity *identitypb.Identity, check func(context.Context) error) string {
	t.Helper()
	id := uuid.NewString()
	spec := &sessionpb.SessionSpec{}
	_, err := store.CreateSessionWithPlacementForCallerAndCheck(t.Context(), id, "test-sandbox", sandboxUID,
		testPlacement(spec, "test-sandbox", sandboxUID), spec, nil, nil,
		SessionExecution{Identity: identity},
		IdentityCaller{Identity: identity, Proof: IdentitySVID{}}, check)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func TestIdentityChangeRefusesRetainedSessionsByNameAndUID(t *testing.T) {
	store := testStore(t, 10)
	sandboxUID := uuid.NewString()
	current := &identitypb.Identity{Name: "test-agent", Uid: uuid.NewString()}
	otherName := &identitypb.Identity{Name: "other-agent", Uid: uuid.NewString()}
	otherUID := &identitypb.Identity{Name: current.Name, Uid: uuid.NewString()}
	id := seedIdentitySession(t, store, sandboxUID, current, nil)
	for _, destination := range []*identitypb.Identity{otherName, otherUID, nil} {
		patched := false
		err := store.ChangeSandboxIdentity(t.Context(), sandboxUID, destination, func() error { patched = true; return nil })
		var conflict *SessionIdentityConflict
		if !errors.As(err, &conflict) || len(conflict.SessionIDs) != 1 || conflict.SessionIDs[0] != id || patched {
			t.Fatalf("destination %v: conflict=%v, patched=%t", destination, err, patched)
		}
	}
	patched := false
	if err := store.ChangeSandboxIdentity(t.Context(), sandboxUID, current, func() error { patched = true; return nil }); err != nil || !patched {
		t.Fatalf("same Identity patch = %v, patched=%t", err, patched)
	}
}

func TestIdentityChangeSerializesWithSessionAdmissionAndPostLockRecheck(t *testing.T) {
	store := testStore(t, 10)
	sandboxUID := uuid.NewString()
	old := &identitypb.Identity{Name: "test-old-agent", Uid: uuid.NewString()}
	next := &identitypb.Identity{Name: "test-next-agent", Uid: uuid.NewString()}
	var stateMu sync.Mutex
	current := old
	patchEntered := make(chan struct{})
	releasePatch := make(chan struct{})
	release := sync.OnceFunc(func() { close(releasePatch) })
	defer release()
	changeDone := make(chan error, 1)
	go func() {
		changeDone <- store.ChangeSandboxIdentity(t.Context(), sandboxUID, next, func() error {
			close(patchEntered)
			<-releasePatch
			stateMu.Lock()
			current = next
			stateMu.Unlock()
			return nil
		})
	}()
	<-patchEntered
	createDone := make(chan error, 1)
	go func() {
		account := serviceAccountIdentityFor(caller, "test-account-uid")
		spec := &sessionpb.SessionSpec{}
		_, err := store.CreateSessionWithPlacementForCallerAndCheck(t.Context(), uuid.NewString(), "test-sandbox", sandboxUID,
			testPlacement(spec, "test-sandbox", sandboxUID), spec, nil, nil,
			SessionExecution{Identity: old},
			ServiceAccountCaller{ServiceAccountIdentity: account, Attribution: &AdmissionAttribution{Actor: account, EffectiveCaller: account}},
			func(context.Context) error {
				stateMu.Lock()
				defer stateMu.Unlock()
				if !sameIdentity(current, old) {
					return status.Error(codes.FailedPrecondition, "sandbox execution identity changed")
				}
				return nil
			})
		createDone <- err
	}()
	waitForAdvisoryWaiter(t, store)
	release()
	if err := <-changeDone; err != nil {
		t.Fatal(err)
	}
	if err := <-createDone; status.Code(apiError(err)) != codes.FailedPrecondition {
		t.Fatalf("post-lock identity recheck = %v, API error = %v", err, apiError(err))
	}
}

func TestSessionAdmissionBeforeIdentityChangeRefusesPatch(t *testing.T) {
	store := testStore(t, 10)
	sandboxUID := uuid.NewString()
	old := &identitypb.Identity{Name: "test-old-agent", Uid: uuid.NewString()}
	next := &identitypb.Identity{Name: "test-next-agent", Uid: uuid.NewString()}
	id := uuid.NewString()
	checkEntered := make(chan struct{})
	releaseCreate := make(chan struct{})
	release := sync.OnceFunc(func() { close(releaseCreate) })
	defer release()
	createDone := make(chan error, 1)
	go func() {
		account := serviceAccountIdentityFor(caller, "test-account-uid")
		spec := &sessionpb.SessionSpec{}
		_, err := store.CreateSessionWithPlacementForCallerAndCheck(t.Context(), id, "test-sandbox", sandboxUID,
			testPlacement(spec, "test-sandbox", sandboxUID), spec, nil, nil,
			SessionExecution{Identity: old},
			ServiceAccountCaller{ServiceAccountIdentity: account, Attribution: &AdmissionAttribution{Actor: account, EffectiveCaller: account}},
			func(context.Context) error { close(checkEntered); <-releaseCreate; return nil })
		createDone <- err
	}()
	<-checkEntered
	changeDone := make(chan error, 1)
	patched := false
	go func() {
		changeDone <- store.ChangeSandboxIdentity(t.Context(), sandboxUID, next, func() error { patched = true; return nil })
	}()
	waitForAdvisoryWaiter(t, store)
	release()
	if err := <-createDone; err != nil {
		t.Fatal(err)
	}
	var conflict *SessionIdentityConflict
	if err := <-changeDone; !errors.As(err, &conflict) || len(conflict.SessionIDs) != 1 || conflict.SessionIDs[0] != id || patched {
		t.Fatalf("post-admission identity change = %v, patched=%t", err, patched)
	}
}

func TestIdentityChangeCheckDoesNotRejectExistingSessionReceipt(t *testing.T) {
	store := testStore(t, 10)
	sandboxUID := uuid.NewString()
	identity := &identitypb.Identity{Name: "test-agent", Uid: uuid.NewString()}
	id := seedIdentitySession(t, store, sandboxUID, identity, nil)
	spec := &sessionpb.SessionSpec{}
	checked := false
	_, err := store.CreateSessionWithPlacementForCallerAndCheck(t.Context(), id, "test-sandbox", sandboxUID,
		testPlacement(spec, "test-sandbox", sandboxUID), spec, nil, nil,
		SessionExecution{Identity: identity},
		IdentityCaller{Identity: identity, Proof: IdentitySVID{}},
		func(context.Context) error { checked = true; return status.Error(codes.FailedPrecondition, "changed") })
	if err != nil || checked {
		t.Fatalf("existing receipt = %v, check called=%t", err, checked)
	}
}
