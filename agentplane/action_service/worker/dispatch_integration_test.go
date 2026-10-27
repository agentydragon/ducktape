package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"slices"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/google/uuid"
)

func TestAnAllowedRequestRunsOnceAndItsOutcomeIsRecorded(t *testing.T) {
	f := newFixture(t)
	// Allowed before the worker listens, so only its first read finds it.
	first := f.allowed(t, "first")
	e := &executor{step: succeed}
	run(t, f.store, e, testTiming(), f.dispatches(t))
	f.await(t, first, action.StateSucceeded)

	// Allowed once the worker waits, so only the dispatch notification wakes it.
	second := f.allowed(t, "second")
	got := f.await(t, second, action.StateSucceeded)
	sameJSON(t, got.Execution.Result, echoed)
	if ran := e.ran(); !slices.Equal(ran, []uuid.UUID{first, second}) {
		t.Errorf("ran %v, want %v", ran, []uuid.UUID{first, second})
	}
	e.mu.Lock()
	payload := e.runs[1]
	e.mu.Unlock()
	if payload.Action != action || payload.Caller != caller.Account {
		t.Errorf("the executor ran %+v", payload)
	}
	sameJSON(t, payload.Arguments, json.RawMessage(`{"key": "second"}`))
	events, err := f.store.Events(t.Context(), second, operator, store.EventsFilter{})
	if err != nil {
		t.Fatal(err)
	}
	want := []action.State{
		action.StateDecisionPending, action.StateAllowed, action.StateDispatching, action.StateRunning, action.StateSucceeded,
	}
	if states := eventStates(events); !slices.Equal(states, want) {
		t.Errorf("events %v, want %v", states, want)
	}
}

func eventStates(events []store.Event) []action.State {
	states := make([]action.State, len(events))
	for i, event := range events {
		states[i] = event.State
	}
	return states
}

func TestEveryWakeupRereadsEveryPendingRequest(t *testing.T) {
	f := newFixture(t)
	wakeups := make(chan struct{}, 1)
	e := &executor{step: succeed}
	run(t, f.store, e, testTiming(), wakeups)
	seen := f.allowed(t, "seen")
	wakeups <- struct{}{}
	// Whichever read found it, the wakeup was spent on it: the worker reads again only on the next.
	f.await(t, seen, action.StateSucceeded)

	lost := f.allowed(t, "lost")
	woken := f.allowed(t, "woken")
	wakeups <- struct{}{}
	f.await(t, lost, action.StateSucceeded)
	f.await(t, woken, action.StateSucceeded)
	if ran := e.ran(); len(ran) != 3 || !slices.Contains(ran, lost) || !slices.Contains(ran, woken) {
		t.Errorf("ran %v", ran)
	}
}

func TestAFailedClaimIsRetriedWithoutAnotherWakeup(t *testing.T) {
	f := newFixture(t)
	id := f.allowed(t, "retried")
	release := f.lock(t, lockRequest, id)

	// The worker's own connections give up on a lock after a moment, so its claim fails while the request is held.
	impatient, err := url.Parse(f.url)
	if err != nil {
		t.Fatal(err)
	}
	query := impatient.Query()
	query.Set("lock_timeout", "100ms")
	impatient.RawQuery = query.Encode()
	db := open(t, impatient.String())
	run(t, store.New(db), &executor{step: succeed}, testTiming(), nil)

	f.awaitCount(t, 1, lockWaiters)
	f.awaitCount(t, 0, lockWaiters)
	release()
	f.await(t, id, action.StateSucceeded)
}

func TestAnExecutionWaitsForItsExecutorToBeReady(t *testing.T) {
	f := newFixture(t)
	id := f.allowed(t, "parked")
	wakeups := make(chan struct{}, 1)
	e := &executor{step: succeed}
	e.unready.Store(true)
	r := run(t, f.store, e, testTiming(), wakeups)
	eventually(t, func() (bool, string) { return e.routed.Load() > 0, "the worker never routed the request" })
	r.awaitIdle(t)
	if got := f.get(t, id); got.State != action.StateAllowed || !slices.Equal(f.pending(t), []uuid.UUID{id}) {
		t.Fatalf("a request whose executor is not ready is %s", got.State)
	}

	e.unready.Store(false)
	wakeups <- struct{}{}
	f.await(t, id, action.StateSucceeded)
}

// The wakeup that says the Executor is ready can come while a dispatch that found it unready is still under way.
func TestAWakeupDuringADispatchRunsTheRequestOnceTheDispatchEnds(t *testing.T) {
	f := newFixture(t)
	parked := f.allowed(t, "parked")
	e := &executor{step: succeed}
	e.unready.Store(true)
	held := &heldRoute{executor: e, routing: make(chan struct{}), resume: make(chan struct{})}
	wakeups := make(chan struct{}, 1)
	run(t, f.store, held, testTiming(), wakeups)
	<-held.routing

	e.unready.Store(false)
	probe := f.allowed(t, "probe")
	wakeups <- struct{}{}
	// The read after the wakeup found parked under way, then started the younger probe.
	f.await(t, probe, action.StateSucceeded)
	close(held.resume)
	f.await(t, parked, action.StateSucceeded)
}

// heldRoute routes like its executor, but holds the first Route until resume closes, answering as things stood when
// it was called. It signals routing when the first Route is called.
type heldRoute struct {
	*executor
	routing, resume chan struct{}
	calls           atomic.Int64
}

func (h *heldRoute) Route(action store.ActionIdentity) (Executor, bool) {
	executor, ready := h.executor.Route(action)
	if h.calls.Add(1) == 1 {
		close(h.routing)
		<-h.resume
	}
	return executor, ready
}

// unrouted is a deployment with no Executor for any Action.
type unrouted struct{}

func (unrouted) Route(store.ActionIdentity) (Executor, bool) { return nil, false }

func TestAnActionNoExecutorRunsFailsOnceStarted(t *testing.T) {
	f := newFixture(t)
	id := f.allowed(t, "unsupported")
	run(t, f.store, unrouted{}, testTiming(), nil)
	got := f.await(t, id, action.StateFailed)
	if kind := errorKind(t, got); kind != "*worker.UnsupportedActionError" {
		t.Errorf("kind %q", kind)
	}
	events, err := f.store.Events(t.Context(), id, operator, store.EventsFilter{})
	if err != nil {
		t.Fatal(err)
	}
	// As in the Python service, the Execution crossed the no-replay boundary before its Executor was resolved.
	if !slices.Contains(eventStates(events), action.StateRunning) {
		t.Errorf("events %v", eventStates(events))
	}
}

type testError struct{}

func (*testError) Error() string { return "test error with request material" }

func TestWhatTheExecutorAnswersIsWhatTheExecutionRecords(t *testing.T) {
	described := json.RawMessage(`{"kind": "test_failure", "message": "test message"}`)
	for name, test := range map[string]struct {
		answer func() (store.ExecutionOutcome, error)
		state  action.State
		kind   string
	}{
		"a described failure": {
			answer: func() (store.ExecutionOutcome, error) {
				return store.ExecutionOutcome{State: store.ExecutionFailed, Error: described}, nil
			},
			state: action.StateFailed, kind: "test_failure",
		},
		"an error, by its type": {
			answer: func() (store.ExecutionOutcome, error) { return store.ExecutionOutcome{}, &testError{} },
			state:  action.StateFailed, kind: "*worker.testError",
		},
		"an unknown outcome": {
			answer: func() (store.ExecutionOutcome, error) {
				return store.ExecutionOutcome{}, fmt.Errorf("test backend timed out: %w", ErrOutcomeUnknown)
			},
			state: action.StateExecutionUnknown, kind: string(store.UnknownAdapterOutcome),
		},
		"an invalid outcome": {
			answer: func() (store.ExecutionOutcome, error) {
				return store.ExecutionOutcome{State: store.ExecutionRunning}, nil
			},
			state: action.StateFailed, kind: "*worker.InvalidOutcomeError",
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			id := f.allowed(t, "answered")
			step := func(context.Context, store.ExecutionRequest, *Lease) (store.ExecutionOutcome, error) {
				return test.answer()
			}
			run(t, f.store, &executor{step: step}, testTiming(), nil)
			got := f.await(t, id, test.state)
			if kind := errorKind(t, got); kind != test.kind {
				t.Errorf("kind %q, want %q", kind, test.kind)
			}
		})
	}
}
