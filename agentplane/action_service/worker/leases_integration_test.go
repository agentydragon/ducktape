package worker

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
)

func TestTheWorkerProvesItsExecutorAlive(t *testing.T) {
	f := newFixture(t)
	r := run(t, f.store, &executor{step: succeed}, testTiming(), nil)
	// A second heartbeat moved it past the first.
	f.awaitCount(t, 1, `SELECT count(*) FROM action_executor_heartbeat WHERE executor_id = $1 AND heartbeat_at > started_at`,
		r.worker.executorID)
}

func TestTheSweepEndsALapsedLeaseOfAnyExecutor(t *testing.T) {
	for _, reason := range []store.UnknownOutcomeReason{store.UnknownExecutorLost, store.UnknownLeaseExpired} {
		t.Run(string(reason), func(t *testing.T) {
			f := newFixture(t)
			id := f.allowed(t, "lapsed")
			if reason == store.UnknownLeaseExpired {
				if err := f.store.RecordExecutorHeartbeat(t.Context(), "test-other-executor"); err != nil {
					t.Fatal(err)
				}
			}
			f.claimElsewhere(t, id, "test-other-executor", -time.Second)
			e := &executor{step: succeed}
			run(t, f.store, e, testTiming(), nil)
			if kind := errorKind(t, f.await(t, id, action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN)); kind != string(reason) {
				t.Errorf("kind %q", kind)
			}
			if ran, pending := e.ran(), f.pending(t); len(ran) != 0 || len(pending) != 0 {
				t.Errorf("replayed: ran %v, pending %v", ran, pending)
			}
		})
	}
}

func TestAnExecutionThatOutlivesItsLeaseEndsUnknownAndItsOutcomeReconcilesIt(t *testing.T) {
	f := newFixture(t)
	id := f.allowed(t, "outlived")
	g := newGate()
	timing := testTiming()
	timing.Lease = time.Second
	// The gate never renews the lease.
	run(t, f.store, &executor{step: g.step}, timing, nil)
	g.awaitStarted(t)
	// The worker's own heartbeat says its executor lives: only the lease lapsed.
	if kind := errorKind(t, f.await(t, id, action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN)); kind != string(store.UnknownLeaseExpired) {
		t.Errorf("kind %q", kind)
	}

	close(g.release)
	got := f.await(t, id, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)
	if got.Execution.ReconciledAt == nil {
		t.Errorf("execution %+v", got.Execution)
	}
	sameJSON(t, got.Execution.Result, echoed)
}

// held runs the gate's step under Hold.
func (g *gate) held(ctx context.Context, request store.ExecutionRequest, lease *Lease) (store.ExecutionOutcome, error) {
	return Hold(ctx, lease, func(ctx context.Context) (store.ExecutionOutcome, error) { return g.step(ctx, request, lease) })
}

func TestHoldRenewsTheLeaseSoTheSweepPassesItBy(t *testing.T) {
	f := newFixture(t)
	id := f.allowed(t, "held")
	g := newGate()
	timing := testTiming()
	timing.Lease = 1500 * time.Millisecond
	run(t, f.store, &executor{step: g.held}, timing, nil)
	g.awaitStarted(t)

	// Claimed after the held Execution and never renewed, its lease lapses after the held one's first would have,
	// and the sweep that ends it has passed the held Execution by.
	sentinel := f.allowed(t, "sentinel")
	f.claimElsewhere(t, sentinel, "test-other-executor", timing.Lease)
	f.await(t, sentinel, action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN)
	if got := f.get(t, id); got.State != action_servicepb.ActionState_ACTION_STATE_RUNNING {
		t.Fatalf("the held request is %s", got.State)
	}

	close(g.release)
	f.await(t, id, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)
}

func TestHoldEndsTheWorkWhenItCannotRenewTheLease(t *testing.T) {
	f := newFixture(t)
	id := f.allowed(t, "unrenewable")
	started, ended := make(chan struct{}), make(chan struct{})
	step := func(ctx context.Context, _ store.ExecutionRequest, lease *Lease) (store.ExecutionOutcome, error) {
		return Hold(ctx, lease, func(ctx context.Context) (store.ExecutionOutcome, error) {
			close(started)
			<-ctx.Done()
			close(ended)
			return store.ExecutionOutcome{}, ctx.Err()
		})
	}
	timing := DefaultTiming()
	// A renewal waits at most a second; the lease outlasts the test, and the sweep is not due within it.
	timing.Lease = 3 * time.Second
	run(t, f.store, &executor{step: step}, timing, nil)
	select {
	case <-started:
	case <-time.After(awaitBound):
		t.Fatal("the work never started")
	}

	release := f.lock(t, lockExecution, id)
	select {
	case <-ended:
	case <-time.After(awaitBound):
		t.Fatal("the work outlived a renewal that could not happen")
	}
	release()
	if kind := errorKind(t, f.await(t, id, action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN)); kind != string(store.UnknownAdapterOutcome) {
		t.Errorf("kind %q", kind)
	}
}
