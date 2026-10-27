package worker

import (
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestADrainLetsTheExecutionInFlightFinishAndClaimsNothingNew(t *testing.T) {
	f := newFixture(t)
	inFlight := f.allowed(t, "in-flight")
	g := newGate()
	e := &executor{step: g.step}
	wakeups := make(chan struct{}, 1)
	r := run(t, f.store, e, testTiming(), wakeups)
	g.awaitStarted(t)

	r.drain()
	after := f.allowed(t, "allowed-during-the-drain")
	wakeups <- struct{}{}
	select {
	case <-r.done:
		t.Fatal("the worker stopped with an Execution in flight")
	default:
	}
	close(g.release)
	r.wait(t)
	if got := f.get(t, inFlight); got.State != action.StateSucceeded {
		t.Errorf("the Execution in flight is %s", got.State)
	}
	if got := f.get(t, after); got.State != action.StateAllowed || !slices.Equal(e.ran(), []uuid.UUID{inFlight}) {
		t.Errorf("a request allowed during the drain is %s; ran %v", got.State, e.ran())
	}
}

func TestADrainFencesAClaimWaitingForItsRequest(t *testing.T) {
	f := newFixture(t)
	id := f.allowed(t, "fenced")
	release := f.lock(t, lockRequest, id)
	e := &executor{step: succeed}
	r := run(t, f.store, e, testTiming(), nil)
	f.awaitCount(t, 1, lockWaiters)

	r.drain()
	release()
	r.wait(t)
	if got := f.get(t, id); got.State != action.StateAllowed || len(e.ran()) != 0 {
		t.Errorf("the request is %s; ran %v", got.State, e.ran())
	}
}

func TestAnExecutionPastTheDrainIsStoppedUnknownAndNeverReplayed(t *testing.T) {
	f := newFixture(t)
	stopped := f.allowed(t, "stopped")
	g := newGate()
	timing := testTiming()
	timing.Drain = 0
	r := run(t, f.store, &executor{step: g.step}, timing, nil)
	g.awaitStarted(t)
	r.drain()
	r.wait(t)
	if got := f.get(t, stopped); got.State != action.StateExecutionUnknown || errorKind(t, got) != string(store.UnknownCoordinatorStopped) {
		t.Errorf("the stopped request is %s: %s", got.State, got.Execution.Error)
	}
	if pending := f.pending(t); len(pending) != 0 {
		t.Errorf("pending %v", pending)
	}

	// The next worker runs only what still awaits dispatch.
	next := f.allowed(t, "next")
	e := &executor{step: succeed}
	run(t, f.store, e, testTiming(), nil)
	f.await(t, next, action.StateSucceeded)
	if got := f.get(t, stopped); got.State != action.StateExecutionUnknown || !slices.Equal(e.ran(), []uuid.UUID{next}) {
		t.Errorf("the stopped request is %s; ran %v", got.State, e.ran())
	}
}

func TestConcurrentWorkersRunEachRequestOnce(t *testing.T) {
	f := newFixture(t)
	var ids []uuid.UUID
	var locked pq.StringArray
	for _, key := range []string{"a", "b", "c", "d", "e"} {
		id := f.allowed(t, "contended-"+key)
		ids, locked = append(ids, id), append(locked, id.String())
	}
	// Both workers queue for every request before either can claim one.
	release := f.lock(t, `SELECT 1 FROM action_request WHERE id = ANY($1::uuid[]) FOR UPDATE`, locked)
	e := &executor{step: succeed}
	run(t, f.store, e, testTiming(), nil)
	run(t, f.store, e, testTiming(), nil)
	f.awaitCount(t, 2*len(ids), lockWaiters)

	release()
	for _, id := range ids {
		f.await(t, id, action.StateSucceeded)
	}
	ran := e.ran()
	slices.SortFunc(ran, func(a, b uuid.UUID) int { return slices.Compare(a[:], b[:]) })
	want := slices.Clone(ids)
	slices.SortFunc(want, func(a, b uuid.UUID) int { return slices.Compare(a[:], b[:]) })
	if !slices.Equal(ran, want) {
		t.Errorf("ran %v, want each of %v once", ran, ids)
	}
}
