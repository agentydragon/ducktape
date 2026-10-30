package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/client-go/util/workqueue"
)

var (
	ErrWakeQuiescing   = errors.New("Sandbox is quiescing")
	ErrWakeModeUnknown = errors.New("Sandbox operating mode unknown")
	ErrWakeDeleted     = errors.New("Sandbox is tearing down")
	ErrWakeSuperseded  = errors.New("wake ownership or queued request was superseded")
)

const deliveryWorkerCount = 4

type wakeLimitedError struct{ retryAfter time.Duration }

type retryKind uint8

const (
	retryFailure retryKind = iota
	retryWakeBudget
)

type retryState struct {
	workID string
	until  time.Time
	kind   retryKind
}

func (e wakeLimitedError) Error() string {
	return fmt.Sprintf("wake budget exhausted for %s", e.retryAfter)
}

// SandboxCatalog must return a complete listing after synchronization. It also
// drives the live sender's name->UID lookup; it does not derive session identity
// from History or a process-local runner list.
type SandboxCatalog interface {
	SandboxReader
	ListSandboxes(context.Context) ([]SandboxSnapshot, error)
	Changes() <-chan struct{}
	WaitSynced(context.Context) bool
}

// WakeController rereads the Sandbox and uses UID/resourceVersion preconditions.
// beforeResume is called only immediately before a guarded patch; it reserves
// the caller's persistent budget and starts the stored wake deadline first.
type WakeController interface {
	WakeForSend(context.Context, string, string, func(int64) error) (bool, error)
}

type DeliveryRunner interface {
	RunnerCommands
	OpenSession(context.Context, SandboxSnapshot, string, *runnerpb.SessionSpec, *string) error
}

type LeaseOwner interface {
	Acquire(context.Context, string, string) (*Owner, error)
	StillOwns(context.Context, Owner) (bool, error)
}

type DeliveryWorker struct {
	Store   *Store
	Catalog SandboxCatalog
	Wake    WakeController
	Leases  LeaseOwner
	Runner  DeliveryRunner
	Hub     *WatchHub
	Logger  *slog.Logger
	// Ready closes after the first successful, complete reconciliation. Serving
	// must wait for this so admission never precedes queue recovery.
	Ready chan struct{}

	mu      sync.Mutex
	owners  map[string]Owner
	refused map[string]string
	queue   workqueue.TypedRateLimitingInterface[string]
	limiter workqueue.TypedRateLimiter[string]
	// A delayed item can be added early by a database notification. The queue
	// owns the timer and retry count; the cause decides whether a newly ready
	// Sandbox may bypass a wake-budget delay.
	retries map[string]retryState
}

func (w *DeliveryWorker) init() {
	if w.Logger == nil {
		w.Logger = slog.Default()
	}
	w.owners = map[string]Owner{}
	w.refused = map[string]string{}
	w.limiter = workqueue.NewTypedItemExponentialFailureRateLimiter[string](500*time.Millisecond, 30*time.Second)
	w.queue = workqueue.NewTypedRateLimitingQueue(w.limiter)
	w.retries = map[string]retryState{}
}

func (w *DeliveryWorker) Run(ctx context.Context) error {
	if w.Store == nil || w.Catalog == nil || w.Wake == nil || w.Leases == nil || w.Runner == nil || w.Hub == nil {
		return errors.New("dispatcher delivery dependencies are required")
	}
	w.init()
	defer w.queue.ShutDown()
	var ready sync.Once
	recovered := false
	if !w.Catalog.WaitSynced(ctx) {
		if err := ctx.Err(); err != nil {
			return err
		}
		return errors.New("Sandbox inventory did not synchronize")
	}
	// Startup may race the listener's initial connection. Never run without
	// notifications indefinitely: retry subscription, and retain the 10s sweep.
	var sub *watchSubscription
	var unsubscribe func()
	for sub == nil {
		var err error
		sub, unsubscribe, err = w.Hub.subscribeChanges()
		if err == nil {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
	defer unsubscribe()
	// A queue key is a Session ID, so even with multiple workers an opening
	// and its commands cannot be delivered concurrently in this process.
	for range deliveryWorkerCount {
		go w.runQueue(ctx)
	}
	renew := true
	ticker := time.NewTicker(LeaseRenewInterval)
	defer ticker.Stop()
	for {
		if !recovered {
			renew = true
		}
		if err := w.reconcile(ctx, renew); err != nil {
			w.Logger.Warn("dispatcher delivery reconciliation unavailable; retrying", "error", err)
			renew = true
		} else {
			if renew {
				recovered = true
				if w.Ready != nil {
					ready.Do(func() { close(w.Ready) })
				}
			}
			renew = false
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			renew = true
		case <-sub.changed:
		case <-w.Catalog.Changes():
		case message := <-sub.failure:
			return fmt.Errorf("dispatcher listener lost notifications: %s", message)
		}
	}
}

func (w *DeliveryWorker) reconcile(ctx context.Context, renew bool) (result error) {
	// A partial scan or failed renewal cannot leave an old token available to
	// independently running queue workers. The next successful renewal restores it.
	defer func() {
		if result != nil {
			w.mu.Lock()
			clear(w.owners)
			w.mu.Unlock()
		}
	}()
	sandboxes, err := w.Catalog.ListSandboxes(ctx)
	if err != nil {
		return err
	}
	byName := make(map[string]SandboxSnapshot, len(sandboxes))
	liveUID := make(map[string]struct{}, len(sandboxes))
	for _, sandbox := range sandboxes {
		byName[sandbox.Name] = sandbox
		liveUID[sandbox.UID] = struct{}{}
	}
	storedUID, err := w.Store.ListSandboxUIDs(ctx)
	if err != nil {
		return err
	}
	for uid := range storedUID {
		if _, live := liveUID[uid]; !live {
			if _, err := w.Store.EndSandbox(ctx, uid); err != nil {
				return err
			}
		}
	}
	pending, err := w.Store.SandboxesWithPendingWork(ctx)
	if err != nil {
		return err
	}
	for _, sandbox := range sandboxes {
		if _, needed := pending[sandbox.UID]; !needed {
			continue
		}
		if !renew {
			if _, owned := w.ownerFor(sandbox.UID); owned {
				continue
			}
		}
		owner, err := w.Leases.Acquire(ctx, sandbox.Name, sandbox.UID)
		if err != nil {
			return err
		}
		w.mu.Lock()
		old, had := w.owners[sandbox.UID]
		if owner == nil {
			delete(w.owners, sandbox.UID)
		} else {
			w.owners[sandbox.UID] = *owner
		}
		w.mu.Unlock()
		if owner != nil && (!had || old != *owner) {
			current, err := w.Store.RecoverSandbox(ctx, *owner)
			if err != nil {
				return err
			}
			if !current {
				w.mu.Lock()
				delete(w.owners, sandbox.UID)
				w.mu.Unlock()
			}
		}
	}
	// A Sandbox without pending work is dropped here and no longer renewed, so its
	// Lease lapses; the next work for it reacquires.
	w.mu.Lock()
	for uid := range w.owners {
		_, live := liveUID[uid]
		_, needed := pending[uid]
		if !live || !needed {
			delete(w.owners, uid)
		}
	}
	w.mu.Unlock()
	openings, err := w.Store.OpeningHeads(ctx)
	if err != nil {
		return err
	}
	heads, err := w.Store.Heads(ctx)
	if err != nil {
		return err
	}
	for _, opening := range openings {
		sandbox, live := byName[opening.SandboxName]
		if live && sandbox.UID == opening.SandboxUID {
			w.enqueue(opening.SessionID)
		}
	}
	for _, head := range heads {
		sandbox, live := byName[head.SandboxName]
		if live && sandbox.UID == head.SandboxUID {
			w.enqueue(head.SessionID)
		}
	}
	return nil
}

func (w *DeliveryWorker) ownerFor(uid string) (Owner, bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	owner, okay := w.owners[uid]
	return owner, okay
}

func (w *DeliveryWorker) forgetOwner(uid string, owner Owner) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.owners[uid] == owner {
		delete(w.owners, uid)
	}
}

func (w *DeliveryWorker) enqueue(sessionID string) {
	retry := w.retryFor(sessionID)
	if delay := time.Until(retry.until); delay > 0 && retry.kind != retryWakeBudget {
		w.queue.AddAfter(sessionID, delay)
	} else {
		w.queue.Add(sessionID)
	}
}

func (w *DeliveryWorker) runQueue(ctx context.Context) {
	for {
		sessionID, shutdown := w.queue.Get()
		if shutdown {
			return
		}
		func() {
			defer w.queue.Done(sessionID)
			if ctx.Err() != nil {
				return
			}
			if err := w.processSession(ctx, sessionID); err != nil && ctx.Err() == nil {
				w.Logger.Warn("dispatcher session delivery unavailable; retrying", "session_id", sessionID, "error", err)
				w.backoff(sessionID, w.workFor(sessionID))
			}
		}()
	}
}

func (w *DeliveryWorker) retryFor(sessionID string) retryState {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.retries[sessionID]
}

func (w *DeliveryWorker) workFor(sessionID string) string {
	return w.retryFor(sessionID).workID
}

func (w *DeliveryWorker) processSession(ctx context.Context, sessionID string) error {
	opening, head, err := w.Store.SessionHead(ctx, sessionID)
	if err != nil {
		return err
	}
	if opening == nil && head == nil {
		w.clearRetry(sessionID)
		return nil
	}
	name, uid, workID := "", "", ""
	if opening != nil {
		name, uid, workID = opening.SandboxName, opening.SandboxUID, "opening:"+opening.SessionID
	} else {
		name, uid, workID = head.SandboxName, head.SandboxUID, head.CommandID
	}
	w.mu.Lock()
	if previous := w.retries[sessionID].workID; previous != "" && previous != workID {
		delete(w.retries, sessionID)
		w.queue.Forget(sessionID)
	}
	w.mu.Unlock()
	sandbox, live, err := w.Catalog.GetSandbox(ctx, name)
	if err != nil {
		return err
	}
	if !live || sandbox.UID != uid {
		return nil
	}
	owner, okay := w.ownerFor(uid)
	if !okay {
		return nil
	}
	current, err := w.Leases.StillOwns(ctx, owner)
	if err != nil || !current {
		w.forgetOwner(uid, owner)
		return err
	}
	if opening != nil {
		ready, err := w.prepareOpening(ctx, opening, sandbox, owner)
		if err != nil || !ready {
			return err
		}
		if w.deliverOpening(ctx, opening, sandbox, owner) {
			w.enqueue(sessionID)
		}
	} else {
		ready, err := w.prepareDispatch(ctx, head, sandbox, owner)
		if err != nil || !ready {
			return err
		}
		if w.deliverCommand(ctx, head, sandbox, owner) {
			w.enqueue(sessionID)
		}
	}
	return nil
}

func (w *DeliveryWorker) clearRetry(sessionID string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.retries, sessionID)
	w.queue.Forget(sessionID)
}

func (w *DeliveryWorker) backoff(sessionID, workID string) {
	delay := w.limiter.When(sessionID)
	w.setRetry(sessionID, workID, delay, retryFailure)
}

func (w *DeliveryWorker) setRetry(sessionID, workID string, after time.Duration, kind retryKind) {
	w.mu.Lock()
	w.retries[sessionID] = retryState{workID: workID, until: time.Now().Add(after), kind: kind}
	w.mu.Unlock()
	w.queue.AddAfter(sessionID, after)
}

func (w *DeliveryWorker) refusedHere(uid, incarnation string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	previous, refused := w.refused[uid]
	if !refused {
		return false
	}
	if previous == incarnation {
		return true
	}
	delete(w.refused, uid)
	return false
}

func (w *DeliveryWorker) markRefused(uid, incarnation string) {
	w.mu.Lock()
	w.refused[uid] = incarnation
	w.mu.Unlock()
}

func (w *DeliveryWorker) deliveryFailure(sessionID, workID, uid, incarnation string, err error) (string, string) {
	grpcStatus, okay := status.FromError(err)
	if !okay {
		w.backoff(sessionID, workID)
		return QueueState, "runner_unreachable: " + err.Error()
	}
	switch grpcStatus.Code() {
	case codes.Aborted:
		w.markRefused(uid, incarnation)
		return QueueState, "runner_refused_work"
	case codes.Unavailable, codes.DeadlineExceeded:
		w.backoff(sessionID, workID)
		return QueueState, fmt.Sprintf("runner_unreachable: %s: %s", grpcStatus.Code(), grpcStatus.Message())
	}
	return FailedState, fmt.Sprintf("runner %s: %s", grpcStatus.Code(), grpcStatus.Message())
}

func (w *DeliveryWorker) retryStorage(ctx context.Context, fn func() error) {
	delay := 500 * time.Millisecond
	for ctx.Err() == nil {
		if err := fn(); err == nil {
			return
		} else {
			w.Logger.Warn("dispatcher settlement unavailable; retrying", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		if delay < 30*time.Second {
			delay *= 2
			if delay > 30*time.Second {
				delay = 30 * time.Second
			}
		}
	}
}

func (w *DeliveryWorker) deliverCommand(ctx context.Context, head *Dispatch, sandbox SandboxSnapshot, owner Owner) bool {
	ctx = observability.WithTraceparent(ctx, head.Traceparent)
	ctx, span := observability.Start(
		ctx, "Dispatcher Deliver Command",
		trace.WithAttributes(
			attribute.String("session.id", head.SessionID),
			attribute.String("sandbox.name", head.SandboxName),
		),
	)
	defer span.End()
	claimed, err := w.Store.Settle(ctx, head.CommandID, owner, []string{QueueState}, DeliveringState, "", nil)
	if err != nil {
		span.SetStatus(otelcodes.Error, "dispatch claim failed")
		w.Logger.Error("cannot claim queued dispatch", "command_id", head.CommandID, "error", err)
		return false
	}
	if claimed == nil {
		return false
	}
	admission, callErr := w.Runner.Deliver(ctx, sandbox, head.SessionID, head.Command)
	if callErr != nil {
		span.SetStatus(otelcodes.Error, "runner delivery failed")
	}
	state, reason := AdmittedState, ""
	if callErr != nil {
		state, reason = w.deliveryFailure(head.SessionID, head.CommandID, head.SandboxUID, sandbox.Incarnation, callErr)
	}
	w.retryStorage(ctx, func() error {
		_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{DeliveringState}, state, reason, admission)
		return err
	})
	if state != QueueState {
		w.clearRetry(head.SessionID)
	}
	return true
}

func (w *DeliveryWorker) deliverOpening(ctx context.Context, opening *Opening, sandbox SandboxSnapshot, owner Owner) bool {
	ctx = observability.WithTraceparent(ctx, opening.Traceparent)
	ctx, span := observability.Start(
		ctx, "Dispatcher Deliver Session Opening",
		trace.WithAttributes(
			attribute.String("session.id", opening.SessionID),
			attribute.String("sandbox.name", opening.SandboxName),
		),
	)
	defer span.End()
	key := "opening:" + opening.SessionID
	claimed, err := w.Store.SettleOpening(ctx, opening.SessionID, owner, []string{QueueState}, DeliveringState, "")
	if err != nil {
		span.SetStatus(otelcodes.Error, "session opening claim failed")
		w.Logger.Error("cannot claim queued opening", "session_id", opening.SessionID, "error", err)
		return false
	}
	if claimed == nil {
		return false
	}
	callErr := w.Runner.OpenSession(ctx, sandbox, opening.SessionID, opening.Spec, opening.SetupScript)
	if callErr != nil {
		span.SetStatus(otelcodes.Error, "runner session opening failed")
	}
	state, reason := AdmittedState, ""
	if callErr != nil {
		state, reason = w.deliveryFailure(opening.SessionID, key, opening.SandboxUID, sandbox.Incarnation, callErr)
	}
	w.retryStorage(ctx, func() error {
		_, err := w.Store.SettleOpening(ctx, opening.SessionID, owner, []string{DeliveringState}, state, reason)
		return err
	})
	if state != QueueState {
		w.clearRetry(opening.SessionID)
	}
	return true
}
