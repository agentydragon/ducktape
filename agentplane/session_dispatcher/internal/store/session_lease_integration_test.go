package store

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

func expireSessionLease(t *testing.T, store *Store, owner SessionOwner) {
	t.Helper()
	if _, err := store.DB.ExecContext(t.Context(), `UPDATE session_delivery_lease
		SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=$1 AND holder=$2`, owner.SessionID, owner.Holder); err != nil {
		t.Fatal(err)
	}
}

func TestCleanupLocksMissingSessionLease(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store, databaseURL := testStoreWithURL(t, 3)
	row, err := enqueueWithSession(t, store, ctx, "test-cleanup-no-lease-session", "test-cleanup-sandbox", sandboxUID, input("test-cleanup-command", "test input"), nil)
	if err != nil {
		t.Fatal(err)
	}
	deleter, deleterPID := connectStore(t, ctx, databaseURL)
	acquirer, acquirerPID := connectStore(t, ctx, databaseURL)
	gate, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Rollback()
	var gatePID int
	if err := gate.QueryRowContext(ctx, `SELECT pg_backend_pid() FROM dispatch WHERE command_id=$1 FOR UPDATE`, row.CommandID).Scan(&gatePID); err != nil {
		t.Fatal(err)
	}
	deleted := make(chan error, 1)
	go func() {
		deleted <- deleter.EndSandbox(ctx, sandboxUID)
	}()
	postgres.WaitForBlocker(t, ctx, store.DB, deleterPID, gatePID)
	type acquisitionResult struct {
		owner *SessionOwner
		err   error
	}
	acquired := make(chan acquisitionResult, 1)
	go func() {
		owner, err := acquirer.AcquireSessionLease(ctx, row.SessionID)
		acquired <- acquisitionResult{owner, err}
	}()
	// Cleanup has created and locked the previously missing lease before waiting
	// on the dispatch row. A concurrent acquisition must wait on it too.
	postgres.WaitForBlocker(t, ctx, store.DB, acquirerPID, deleterPID)
	if err := gate.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := <-deleted; err != nil {
		t.Fatal(err)
	}
	var state string
	if err := store.DB.QueryRowContext(ctx, "SELECT state FROM dispatch WHERE command_id=$1", row.CommandID).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != FailedState {
		t.Fatalf("unsent cleanup state = %s, want %s", state, FailedState)
	}
	successor := <-acquired
	if successor.err != nil || successor.owner == nil {
		t.Fatalf("acquire after cleanup: %v %v", successor.owner, successor.err)
	}
	defer store.ReleaseSessionLease(context.Background(), *successor.owner)
	if marked, err := store.MarkDeliveryAttempt(ctx, row.CommandID, *successor.owner); err != nil || marked {
		t.Fatalf("cleanup permitted forwarding: %t %v", marked, err)
	}
}

func TestSessionLeaseExpiryFencesAttemptsSettlementAndRecovery(t *testing.T) {
	store := testStore(t, 3)
	ctx := t.Context()
	row, err := enqueueWithSession(t, store, ctx, "test-lease-session", "test-lease-sandbox", sandboxUID, input("test-lease-command", "test input"), nil)
	if err != nil {
		t.Fatal(err)
	}
	stale := claimSessionOwner(t, store, row.SessionID)
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
			successor := claimSessionOwner(t, store, row.SessionID)
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
	if err != nil || got.State != QueueState || got.Reason != "dispatcher_restarted" || !got.DeliveryAttempted {
		t.Fatalf("takeover did not requeue the dispatch with its attempt evidence: %v %v", got, err)
	}
}

func TestSessionLeaseHandoffRecoversOnlyOwnedSession(t *testing.T) {
	store := testStore(t, 3)
	ctx := t.Context()
	const recoveringID, siblingID = "test-recover-session", "test-live-session"
	var stale SessionOwner
	for _, id := range []string{recoveringID, siblingID} {
		if _, err := createSession(store, ctx, id, "test-shared-sandbox", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
		owner := claimSessionOwner(t, store, id)
		if row, err := store.SettleSession(ctx, id, owner, []string{SessionWaitingState}, SessionInProgressState, ""); err != nil || row == nil {
			t.Fatalf("claim Session: %v %v", row, err)
		}
		if id == recoveringID {
			stale = owner
		}
	}
	expireSessionLease(t, store, stale)
	successor := claimSessionOwner(t, store, recoveringID)
	if row, err := store.SettleSession(ctx, recoveringID, stale, []string{SessionInProgressState}, SessionAcceptedState, "stale"); err != nil || row != nil {
		t.Fatalf("stale holder settled after lease handoff: %v %v", row, err)
	}
	if recovered, err := store.RecoverSession(ctx, successor); err != nil || !recovered {
		t.Fatalf("recover: %t %v", recovered, err)
	}
	if row, err := store.GetSession(ctx, siblingID); err != nil || row.State != SessionInProgressState {
		t.Fatalf("recovery changed sibling: %v %v", row, err)
	}
	if row, err := store.GetSession(ctx, recoveringID); err != nil || row.State != SessionWaitingState || row.Reason != "dispatcher_restarted" {
		t.Fatalf("recovery did not requeue Session: %v %v", row, err)
	}
}

func TestSessionLeaseDoesNotRequireAnEnvironment(t *testing.T) {
	store := testStore(t, 3)
	ctx := t.Context()
	const id = "test-session-without-environment"
	if _, err := createSession(store, ctx, id, "test-deleted-environment", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	if err := store.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	owner, err := store.AcquireSessionLease(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RenewSessionLease(ctx, *owner); err != nil {
		t.Fatal(err)
	}
	if err := store.ReleaseSessionLease(context.Background(), *owner); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcquireSessionLease(ctx, id); err != nil {
		t.Fatalf("released environment-free Session is not leasable: %v", err)
	}
}

func TestPendingScanRecoversStrandedWorkWithoutLeaseRows(t *testing.T) {
	store := testStore(t, 3)
	ctx := t.Context()
	const openingID, commandSessionID = "test-stranded-opening", "test-stranded-command-session"
	if _, err := createSession(store, ctx, openingID, "test-stranded-sandbox", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	row, err := enqueueWithSession(t, store, ctx, commandSessionID, "test-stranded-sandbox", sandboxUID, input("test-stranded-command", "test input"), nil)
	if err != nil {
		t.Fatal(err)
	}
	// Model a crashed pre-expansion worker: its in-flight work has no Session lease row.
	if _, err := store.DB.ExecContext(ctx, `UPDATE session SET state=CASE WHEN session_id=$1 THEN $2 ELSE $3 END`, openingID, SessionInProgressState, SessionAcceptedState); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, `UPDATE dispatch SET state=$1,delivery_attempted=true WHERE command_id=$2`, DeliveringState, row.CommandID); err != nil {
		t.Fatal(err)
	}
	var leaseRows int
	if err := store.DB.QueryRowContext(ctx, `SELECT count(*) FROM session_delivery_lease`).Scan(&leaseRows); err != nil || leaseRows != 0 {
		t.Fatalf("fixture leases: %d %v", leaseRows, err)
	}
	pending, err := store.SessionsWithPendingWork(ctx)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, session := range pending {
		seen[session.SessionID] = true
		owner := claimSessionOwner(t, store, session.SessionID)
		if recovered, err := store.RecoverSession(ctx, owner); err != nil || !recovered {
			t.Fatalf("recover %s: %t %v", session.SessionID, recovered, err)
		}
	}
	if len(seen) != 2 || !seen[openingID] || !seen[commandSessionID] {
		t.Fatalf("scan missed stranded work: %v", seen)
	}
	opening, head, err := store.SessionHead(ctx, openingID)
	if err != nil || opening == nil || opening.State != SessionWaitingState || head != nil {
		t.Fatalf("recovered opening head: %v %v %v", opening, head, err)
	}
	opening, head, err = store.SessionHead(ctx, commandSessionID)
	if err != nil || opening != nil || head == nil || head.State != QueueState || !head.DeliveryAttempted {
		t.Fatalf("recovered command head: %v %v %v", opening, head, err)
	}
}
