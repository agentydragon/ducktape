// Package worker runs the Action Service's Executions. It claims the one dispatch of each allowed request, runs it
// through the Executor of its Action, which renews the lease the claim granted, and records how it ended. Beside
// that it ends the Executions whose lease lapsed as unknown, and proves its executor alive.
//
// The rows are the authority: any replica's worker dispatches any request, an Execution is claimed once and never
// replayed, and an attempt whose process died leaves a lease that the next sweep, on any replica, ends.
package worker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/google/uuid"
	"github.com/sethvargo/go-retry"
)

// Timing is the worker's intervals and bounds.
type Timing struct {
	// Lease is the window a claim or a renewal grants.
	Lease time.Duration
	// Sweep is how often lapsed leases are ended.
	Sweep time.Duration
	// ExecutorHeartbeat is how often the worker proves its executor alive.
	ExecutorHeartbeat time.Duration
	// ExecutorHealthTimeout is how long an executor may stay silent before the sweep records its lapsed leases as
	// the executor lost rather than only the lease expired.
	ExecutorHealthTimeout time.Duration
	// Drain is how long a drain lets the Executions in flight finish before it ends their contexts.
	Drain time.Duration
	// Stop is how long an Execution whose context the drain ended has to record its outcome.
	Stop time.Duration
}

// DefaultTiming is the Python service's.
func DefaultTiming() Timing {
	return Timing{
		Lease: 30 * time.Second, Sweep: 5 * time.Second,
		ExecutorHeartbeat: 10 * time.Second, ExecutorHealthTimeout: 45 * time.Second,
		Drain: 20 * time.Second, Stop: 5 * time.Second,
	}
}

// backoff paces the retries of a failed read or claim, from one second to thirty.
func backoff() retry.Backoff {
	return retry.WithCappedDuration(30*time.Second, retry.NewExponential(time.Second))
}

// Worker runs the Executions of one executor: the process, under one executor ID for its lifetime.
type Worker struct {
	store      *store.Store
	executors  Executors
	executorID string
	timing     Timing

	mu sync.Mutex
	// scheduled holds the requests whose dispatch is under way, so that a wakeup starts none twice. True marks one a
	// wakeup found under way: what woke it, such as its Executor becoming ready, may have come after the dispatch
	// looked, so the dispatch claims once more when it ends.
	scheduled map[uuid.UUID]bool
}

func New(s *store.Store, executors Executors, timing Timing) (*Worker, error) {
	for _, positive := range []time.Duration{timing.Lease, timing.Sweep, timing.ExecutorHeartbeat, timing.ExecutorHealthTimeout, timing.Stop} {
		if positive <= 0 {
			return nil, fmt.Errorf("worker intervals and the stop bound must be positive: %+v", timing)
		}
	}
	if timing.Drain < 0 {
		return nil, fmt.Errorf("the drain bound must not be negative: %+v", timing)
	}
	return &Worker{
		store: s, executors: executors, executorID: "executor-" + uuid.NewString(), timing: timing,
		scheduled: map[uuid.UUID]bool{},
	}, nil
}

// Run works until ctx ends, then drains and returns; call it once.
//
// Run reads every request awaiting dispatch when it starts and after each signal on wakeups: a dispatch committed
// on any replica (store.DispatchChannel), the LISTEN connection's return, since a commit made while it was down is
// only in the table, and an Executor becoming ready. A dispatch whose own wakeup was lost is claimed on the next.
//
// The drain starts when ctx ends. From then on nothing is claimed, though a claim already committed still runs. The
// Executions in flight get timing.Drain to finish; then their contexts end, and they get timing.Stop to record how
// they ended before the worker's writes are cancelled too, leaving their leases to the sweep. The executor
// heartbeat and the sweep run until every dispatch has returned.
func (w *Worker) Run(ctx context.Context, wakeups <-chan struct{}) {
	executions, stopExecutions := context.WithCancel(context.WithoutCancel(ctx))
	defer stopExecutions()
	writes, stopWrites := context.WithCancel(context.WithoutCancel(ctx))
	defer stopWrites()
	background, stopBackground := context.WithCancel(context.WithoutCancel(ctx))
	var loops sync.WaitGroup
	loops.Go(func() { w.heartbeats(background) })
	loops.Go(func() { w.sweeps(background) })
	defer loops.Wait()
	defer stopBackground()

	var dispatches sync.WaitGroup
	w.follow(ctx, wakeups, func(id uuid.UUID) {
		dispatches.Go(func() {
			for again := true; again; again = w.unschedule(id) {
				w.dispatch(ctx, executions, writes, id)
			}
		})
	})
	finished := make(chan struct{})
	go func() {
		dispatches.Wait()
		close(finished)
	}()
	if !within(finished, w.timing.Drain) {
		stopExecutions()
		if !within(finished, w.timing.Stop) {
			stopWrites()
			<-finished
		}
	}
}

// within reports whether done closes within d.
func within(done <-chan struct{}, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-done:
		return true
	case <-timer.C:
	}
	select {
	case <-done:
		return true
	default:
		return false
	}
}

// follow starts a dispatch of every request awaiting one, now and after each wakeup, until ctx ends.
func (w *Worker) follow(ctx context.Context, wakeups <-chan struct{}, start func(uuid.UUID)) {
	for {
		// Clear before reading: a commit that races with the read leaves a wakeup for the next wait.
		select {
		case <-wakeups:
		default:
		}
		pending, err := retry.DoValue(ctx, backoff(), func(ctx context.Context) ([]uuid.UUID, error) {
			pending, err := w.store.PendingDispatches(ctx)
			if err != nil && ctx.Err() == nil {
				// Database errors can carry request material; the type names the failure.
				slog.WarnContext(ctx, "pending dispatch read failed; retrying", "error_type", fmt.Sprintf("%T", err))
				return nil, retry.RetryableError(err)
			}
			return pending, err
		})
		if err != nil {
			return
		}
		for _, id := range pending {
			if w.schedule(id) {
				start(id)
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-wakeups:
		}
	}
}

func (w *Worker) schedule(id uuid.UUID) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, scheduled := w.scheduled[id]; scheduled {
		w.scheduled[id] = true
		return false
	}
	w.scheduled[id] = false
	return true
}

// unschedule ends the dispatch of id, unless a wakeup found it under way: then id stays scheduled, and unschedule
// reports that it is to be dispatched again.
func (w *Worker) unschedule(id uuid.UUID) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.scheduled[id] {
		w.scheduled[id] = false
		return true
	}
	delete(w.scheduled, id)
	return false
}

// dispatch claims the request's Execution and, when this worker won it, runs it and records how it ended. The
// executor sees executions; the store calls use writes, which outlive it by the stop bound.
func (w *Worker) dispatch(ctx, executions, writes context.Context, id uuid.UUID) {
	claim, err := w.claim(ctx, writes, id)
	if err != nil {
		slog.Error("action dispatch coordination failed; request will not be retried",
			"request_id", id, "error_type", fmt.Sprintf("%T", err))
		return
	}
	if claim == nil {
		return
	}
	lease := &Lease{store: w.store, claim: *claim, duration: w.timing.Lease}
	if err := w.finish(writes, lease, w.execute(executions, writes, lease)); err != nil {
		slog.Error("action outcome not recorded; the lease sweep will end it unknown",
			"request_id", id, "error_type", fmt.Sprintf("%T", err))
	}
}

// claim takes the request's one dispatch for this worker, nil when another worker has it, a drain started, or its
// Executor is not ready. A claim that fails is retried with backoff, since nothing else would wake for the request.
func (w *Worker) claim(ctx, writes context.Context, id uuid.UUID) (*store.ExecutionClaim, error) {
	canDispatch := func(action store.ActionIdentity) bool {
		if ctx.Err() != nil {
			return false
		}
		executor, ready := w.executors.Route(action)
		return executor == nil || ready
	}
	attempt := 0
	claim, err := retry.DoValue(ctx, backoff(), func(context.Context) (*store.ExecutionClaim, error) {
		claim, err := w.store.ClaimExecution(writes, id, w.executorID, w.timing.Lease, canDispatch)
		var conflict *store.ConflictError
		if err == nil || errors.Is(err, store.ErrNotFound) || errors.As(err, &conflict) {
			return claim, err
		}
		attempt++
		slog.Warn("action dispatch claim failed; retrying",
			"request_id", id, "error_type", fmt.Sprintf("%T", err), "attempt", attempt)
		return nil, retry.RetryableError(err)
	})
	if claim == nil && ctx.Err() != nil {
		return nil, nil
	}
	return claim, err
}

// execute crosses the no-replay boundary, runs the Execution through its Executor, and returns how it ended.
func (w *Worker) execute(executions, writes context.Context, lease *Lease) store.ExecutionOutcome {
	stopped := unknownOutcome(store.UnknownCoordinatorStopped, stoppedMessage)
	if executions.Err() != nil {
		return stopped
	}
	request, err := w.store.MarkRunning(writes, lease.claim.RequestID)
	if err != nil {
		return w.failed(lease, err)
	}
	if executions.Err() != nil {
		return stopped
	}
	executor, ready := w.executors.Route(request.Action)
	if executor == nil || !ready {
		return w.failed(lease, &UnsupportedActionError{Action: request.Action})
	}
	outcome, err := executor.Execute(executions, request, lease)
	if err == nil {
		if invalid := outcome.Validate(); invalid != nil {
			err = &InvalidOutcomeError{Err: invalid}
		}
	}
	switch {
	case err == nil:
		return outcome
	case executions.Err() != nil:
		return stopped
	case errors.Is(err, ErrOutcomeUnknown):
		return unknownOutcome(store.UnknownAdapterOutcome, unknownMessage)
	default:
		return w.failed(lease, err)
	}
}

func (w *Worker) failed(lease *Lease, err error) store.ExecutionOutcome {
	slog.Warn("executor failed for Action request", "request_id", lease.claim.RequestID, "error", err)
	return failedOutcome(err)
}

// finish records the outcome while renewing the lease: a known outcome is worth delivering even once the lease
// lapsed, when the store takes it as a reconciliation.
func (w *Worker) finish(writes context.Context, lease *Lease, outcome store.ExecutionOutcome) error {
	renewing, stopRenewing := context.WithCancel(writes)
	renewed := make(chan struct{})
	go func() {
		defer close(renewed)
		for sleep(renewing, lease.RenewalInterval()) {
			owned, err := lease.renew(renewing)
			if err != nil && renewing.Err() == nil {
				slog.Warn("completion lease renewal failed; authenticated outcome delivery still pending",
					"request_id", lease.claim.RequestID, "error_type", fmt.Sprintf("%T", err))
			}
			if err != nil || !owned {
				return
			}
		}
	}()
	defer func() {
		stopRenewing()
		<-renewed
	}()
	return w.store.FinishExecution(writes, lease.claim.RequestID, lease.claim.Lease, outcome)
}

// heartbeats proves the executor alive, whether or not it holds an Execution, until ctx ends.
func (w *Worker) heartbeats(ctx context.Context) {
	for {
		if err := w.store.RecordExecutorHeartbeat(ctx, w.executorID); err != nil && ctx.Err() == nil {
			slog.WarnContext(ctx, "executor heartbeat failed; will retry", "error_type", fmt.Sprintf("%T", err))
		}
		if !sleep(ctx, w.timing.ExecutorHeartbeat) {
			return
		}
	}
}

// sweeps ends the Executions whose lease lapsed, on any replica, until ctx ends.
func (w *Worker) sweeps(ctx context.Context) {
	for {
		expired, err := w.store.ExpireStaleLeases(ctx, w.timing.ExecutorHealthTimeout)
		switch {
		case err != nil && ctx.Err() == nil:
			slog.WarnContext(ctx, "lease sweep failed; will retry", "error_type", fmt.Sprintf("%T", err))
		case len(expired) > 0:
			slog.InfoContext(ctx, "lease sweep marked Executions unknown", "count", len(expired))
		}
		if !sleep(ctx, w.timing.Sweep) {
			return
		}
	}
}

// sleep waits for d, or reports false when ctx ends first.
func sleep(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
