package store

import (
	"context"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestVersionWatchCheckpointWaitsForLowerAllocatedVersion(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := testStore(t, 5)
	filter := storeDispatchFilter("test-order-session")
	for _, id := range []string{"test-order-a", "test-order-b"} {
		if _, err := enqueueWithSession(t, store, ctx, filter.GetSessionId(), "test-order-sandbox", sandboxUID, input(id, "test-input"), nil); err != nil {
			t.Fatal(err)
		}
	}
	caller := ServiceCaller{SPIFFEID: peertls.AppURI}
	before, err := store.ReadDispatchChanges(ctx, filter, caller, 0, false)
	if err != nil {
		t.Fatal(err)
	}
	low, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer low.Rollback()
	lowPID := postgres.BackendPID(t, ctx, low)
	if _, err := low.ExecContext(ctx, "UPDATE dispatch SET reason='test-low-version' WHERE command_id='test-order-a'"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, "UPDATE dispatch SET reason='test-high-version' WHERE command_id='test-order-b'"); err != nil {
		t.Fatal(err)
	}
	result := make(chan DispatchChanges, 1)
	failed := make(chan error, 1)
	go func() {
		batch, err := store.ReadDispatchChanges(ctx, filter, caller, before.Through, true)
		result <- batch
		failed <- err
	}()
	for {
		var waiting bool
		if err := store.DB.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM pg_stat_activity
			WHERE datname=current_database() AND query='LOCK TABLE dispatch, dispatch_tombstone IN SHARE MODE'
			AND $1=ANY(pg_blocking_pids(pid)))`, lowPID).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting {
			break
		}
		if ctx.Err() != nil {
			t.Fatal("checkpoint did not wait for earlier writer")
		}
		runtime.Gosched()
	}
	if err := low.Commit(); err != nil {
		t.Fatal(err)
	}
	batch := <-result
	if err := <-failed; err != nil {
		t.Fatal(err)
	}
	if len(batch.Changes) != 2 || batch.Changes[0].Dispatch.GetCommandId() != "test-order-a" || batch.Changes[1].Dispatch.GetCommandId() != "test-order-b" || batch.Changes[0].Version >= batch.Changes[1].Version {
		t.Fatalf("checkpoint skipped or reordered the lower committed version: %+v", batch)
	}
}

func TestVersionWatchSnapshotQueryDoesNotBlockDispatchWriter(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := testStore(t, 5)
	filter := storeDispatchFilter("test-nonblocking-session")
	if _, err := enqueueWithSession(t, store, ctx, filter.GetSessionId(), "test-nonblocking-sandbox", sandboxUID, input("test-nonblocking-command", "test-input"), nil); err != nil {
		t.Fatal(err)
	}
	// Only the batch query joins session. Hold that query here while the frontier
	// transaction can finish, then observe the actual query waiting in PostgreSQL.
	barrier, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer barrier.Rollback()
	barrierPID := postgres.BackendPID(t, ctx, barrier)
	if _, err := barrier.ExecContext(ctx, "LOCK TABLE session IN ACCESS EXCLUSIVE MODE"); err != nil {
		t.Fatal(err)
	}
	type result struct {
		batch DispatchChanges
		err   error
	}
	finished := make(chan result, 1)
	caller := ServiceCaller{SPIFFEID: peertls.AppURI}
	go func() {
		batch, err := store.ReadDispatchChanges(ctx, filter, caller, 0, false)
		finished <- result{batch, err}
	}()
	for {
		var waiting bool
		if err := store.DB.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM pg_stat_activity
			WHERE datname=current_database() AND query LIKE 'WITH changes AS (%'
			AND $1=ANY(pg_blocking_pids(pid)))`, barrierPID).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting {
			break
		}
		select {
		case got := <-finished:
			t.Fatalf("snapshot ended before reaching query barrier: %v", got.err)
		default:
		}
		runtime.Gosched()
	}
	// Complete this write BEFORE releasing the query barrier. No sleeps or large
	// fixture make the overlap probabilistic; retaining SHARE across the query fails.
	var writtenVersion int64
	if err := store.DB.QueryRowContext(ctx, `UPDATE dispatch SET reason='test-concurrent-write'
		WHERE command_id='test-nonblocking-command' RETURNING version`).Scan(&writtenVersion); err != nil {
		t.Fatalf("dispatch writer blocked behind snapshot query: %v", err)
	}
	if err := barrier.Commit(); err != nil {
		t.Fatal(err)
	}
	got := <-finished
	if got.err != nil {
		t.Fatal(got.err)
	}
	if len(got.batch.Changes) != 1 || got.batch.Changes[0].Version > got.batch.Through || writtenVersion <= got.batch.Through {
		t.Fatalf("snapshot escaped its frontier: %+v, writer version %d", got.batch, writtenVersion)
	}
	// A read whose snapshot begins after that update must omit the newer row,
	// rather than emit a frame token beyond its own CaughtUp checkpoint.
	bounded, err := store.readDispatchChangesThrough(ctx, filter, caller, 0, got.batch.Through, false)
	if err != nil || len(bounded.Changes) != 0 {
		t.Fatalf("bounded read included post-frontier update: %+v %v", bounded, err)
	}
	next, err := store.ReadDispatchChanges(ctx, filter, caller, got.batch.Through, true)
	if err != nil || len(next.Changes) != 1 || next.Changes[0].Version != writtenVersion || next.Changes[0].Dispatch.GetReason() != "test-concurrent-write" {
		t.Fatalf("next batch missed concurrent write: %+v %v", next, err)
	}
}

func TestVersionWatchBoundsDeletionAfterFrontierAndRechecksPruning(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 5)
	filter := storeDispatchFilter("test-frontier-deletion-session")
	if _, err := enqueueWithSession(t, store, ctx, filter.GetSessionId(), "test-frontier-deletion-sandbox", sandboxUID, input("test-frontier-deletion-command", "test-input"), nil); err != nil {
		t.Fatal(err)
	}
	caller := ServiceCaller{SPIFFEID: peertls.AppURI}
	through, err := store.dispatchWatchFrontier(ctx, 0, false)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	batch, err := store.readDispatchChangesThrough(ctx, filter, caller, 0, through, false)
	if err != nil || len(batch.Changes) != 0 {
		t.Fatalf("batch included a deletion after its frontier: %+v %v", batch, err)
	}
	next, err := store.ReadDispatchChanges(ctx, filter, caller, through, true)
	if err != nil || len(next.Changes) != 1 || next.Changes[0].Dispatch.GetCommandId() != "test-frontier-deletion-command" || next.Changes[0].Dispatch.GetSandboxUid() != "" {
		t.Fatalf("next batch missed deletion tombstone: %+v %v", next, err)
	}
	// Prune between the unlocked frontier and batch transactions. The second
	// coverage check must reject the cursor rather than checkpoint the missing row.
	frontier, err := store.dispatchWatchFrontier(ctx, through, true)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, "UPDATE dispatch_tombstone SET deleted_at=clock_timestamp()-interval '48 hours'"); err != nil {
		t.Fatal(err)
	}
	if n, err := store.PruneDispatchTombstones(ctx); err != nil || n != 1 {
		t.Fatalf("prune: %d %v", n, err)
	}
	_, err = store.readDispatchChangesThrough(ctx, filter, caller, through, frontier, true)
	if status.Code(err) != codes.OutOfRange || !strings.Contains(status.Convert(err).Message(), "resync") {
		t.Fatalf("pruning between frontier and batch silently lost deletion: %v", err)
	}
}
