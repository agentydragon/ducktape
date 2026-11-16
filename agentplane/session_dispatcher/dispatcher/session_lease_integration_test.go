package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func expireSessionLease(t *testing.T, store *Store, owner SessionOwner) {
	t.Helper()
	if _, err := store.DB.ExecContext(t.Context(), `UPDATE session_delivery_lease
		SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=$1 AND holder=$2`, owner.SessionID, owner.Holder); err != nil {
		t.Fatal(err)
	}
}

func TestSessionLeaseExpiryFencesAttemptsSettlementAndRecovery(t *testing.T) {
	store := testStore(t, 3)
	ctx := t.Context()
	row, err := enqueueWithSession(t, store, ctx, "test-lease-session", "test-lease-sandbox", sandboxUID, input("test-lease-command", "test input"), nil)
	if err != nil {
		t.Fatal(err)
	}
	legacy, err := store.ClaimSandboxOwner(ctx, sandboxUID, "test-lease", 1, "test-replica", 0)
	if err != nil || legacy == nil {
		t.Fatalf("compatibility owner: %v %v", legacy, err)
	}
	stale := claimSessionOwner(t, store, *legacy, row.SessionID)
	if _, err := store.AcquireSessionLease(ctx, row.SessionID); !errors.As(err, new(*lease.HeldError)) {
		t.Fatalf("live lease did not refuse takeover: %v", err)
	}
	if err := store.RenewSessionLease(ctx, stale); err != nil {
		t.Fatal(err)
	}
	if claimed, err := store.Settle(ctx, row.CommandID, stale, []string{QueueState}, DeliveringState, "", nil); err != nil || claimed == nil {
		t.Fatalf("claim: %v %v", claimed, err)
	}
	if marked, err := store.MarkDeliveryAttempt(ctx, row.CommandID, stale); err != nil || !marked {
		t.Fatalf("mark: %t %v", marked, err)
	}
	expireSessionLease(t, store, stale)
	for _, takeover := range []bool{false, true} {
		if takeover {
			successor := claimSessionOwner(t, store, *legacy, row.SessionID)
			if successor.Holder == stale.Holder {
				t.Fatal("takeover reused the fencing token")
			}
			if recovered, err := store.RecoverSession(ctx, successor); err != nil || !recovered {
				t.Fatalf("recover: %t %v", recovered, err)
			}
			if err := store.ReleaseSessionLease(ctx, stale); err != nil {
				t.Fatal(err)
			}
			if err := store.RenewSessionLease(ctx, successor); err != nil {
				t.Fatalf("stale release lost successor: %v", err)
			}
		}
		if err := store.RenewSessionLease(ctx, stale); !errors.Is(err, lease.ErrLost) {
			t.Fatalf("expired renewal: %v", err)
		}
		if marked, err := store.MarkDeliveryAttempt(ctx, row.CommandID, stale); err != nil || marked {
			t.Fatalf("expired holder marked: %t %v", marked, err)
		}
		if changed, err := store.Settle(ctx, row.CommandID, stale, []string{QueueState, DeliveringState}, FailedState, "test stale settlement", nil); err != nil || changed != nil {
			t.Fatalf("expired holder settled: %v %v", changed, err)
		}
		if recovered, err := store.RecoverSession(ctx, stale); err != nil || recovered {
			t.Fatalf("expired holder recovered: %t %v", recovered, err)
		}
	}
	got, err := store.GetDispatch(ctx, row.CommandID)
	if err != nil || got.State != QueueState || !got.DeliveryAttempted {
		t.Fatalf("takeover lost attempt evidence: %v %v", got, err)
	}
}

func TestRecoveryDoesNotTouchAnotherSessionOnTheSameSandbox(t *testing.T) {
	store := testStore(t, 3)
	ctx := t.Context()
	legacy, err := store.ClaimSandboxOwner(ctx, sandboxUID, "test-shared-lease", 1, "test-replica", 0)
	if err != nil || legacy == nil {
		t.Fatalf("compatibility owner: %v %v", legacy, err)
	}
	for _, id := range []string{"test-recover-session", "test-live-session"} {
		if _, err := store.CreateSession(ctx, id, "test-shared-sandbox", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
		owner := claimSessionOwner(t, store, *legacy, id)
		if row, err := store.SettleSession(ctx, id, owner, []string{SessionWaitingState}, SessionInProgressState, ""); err != nil || row == nil {
			t.Fatalf("claim Session: %v %v", row, err)
		}
		if id == "test-recover-session" {
			expireSessionLease(t, store, owner)
		}
	}
	successor := claimSessionOwner(t, store, *legacy, "test-recover-session")
	if recovered, err := store.RecoverSession(ctx, successor); err != nil || !recovered {
		t.Fatalf("recover: %t %v", recovered, err)
	}
	if row, err := store.GetSession(ctx, "test-live-session"); err != nil || row.State != SessionInProgressState {
		t.Fatalf("recovery changed sibling: %v %v", row, err)
	}
	if row, err := store.GetSession(ctx, successor.SessionID); err != nil || row.State != SessionWaitingState {
		t.Fatalf("recovery did not requeue Session: %v %v", row, err)
	}
}

func TestSessionLeaseDoesNotRequireAnEnvironment(t *testing.T) {
	store := testStore(t, 3)
	ctx := t.Context()
	const id = "test-session-without-environment"
	if _, err := store.CreateSession(ctx, id, "test-deleted-environment", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	owner, err := store.AcquireSessionLease(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if err := transaction(ctx, store.DB, func(tx *sql.Tx) error {
		return sessionDeliveryLease.Fence(ctx, tx, owner.lease())
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.ReleaseSessionLease(context.Background(), *owner); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcquireSessionLease(ctx, id); err != nil {
		t.Fatalf("released environment-free Session is not leasable: %v", err)
	}
}
