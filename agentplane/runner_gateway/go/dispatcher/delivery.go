package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var (
	ErrWakeQuiescing   = errors.New("Sandbox is quiescing")
	ErrWakeModeUnknown = errors.New("Sandbox operating mode unknown")
	ErrWakeDeleted     = errors.New("Sandbox is tearing down")
	ErrWakeSuperseded  = errors.New("wake ownership or queued request was superseded")
)

type wakeLimitedError struct{ retryAfter time.Duration }

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

	mu             sync.Mutex
	owners         map[string]Owner
	delivering     map[string]struct{}
	refused        map[string]string
	attempts       map[string]int
	retryAt        map[string]time.Time
	wakeDeadlineAt map[string]time.Time
	done           chan struct{}
}

func (w *DeliveryWorker) init() {
	if w.Logger == nil {
		w.Logger = slog.Default()
	}
	w.owners = map[string]Owner{}
	w.delivering = map[string]struct{}{}
	w.refused = map[string]string{}
	w.attempts = map[string]int{}
	w.retryAt = map[string]time.Time{}
	w.wakeDeadlineAt = map[string]time.Time{}
	w.done = make(chan struct{}, 1)
}

func (w *DeliveryWorker) Run(ctx context.Context) error {
	if w.Store == nil || w.Catalog == nil || w.Wake == nil || w.Leases == nil || w.Runner == nil || w.Hub == nil {
		return errors.New("dispatcher delivery dependencies are required")
	}
	w.init()
	var ready sync.Once
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
		sub, unsubscribe, err = w.Hub.subscribe()
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
	for {
		if err := w.reconcile(ctx); err != nil {
			w.Logger.Warn("dispatcher delivery reconciliation unavailable; retrying", "error", err)
		} else if w.Ready != nil {
			ready.Do(func() { close(w.Ready) })
		}
		wait := w.nextWait()
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		case <-sub.changed:
			timer.Stop()
		case <-w.Catalog.Changes():
			timer.Stop()
		case <-w.done:
			timer.Stop()
		case message := <-sub.failure:
			timer.Stop()
			return fmt.Errorf("dispatcher listener lost notifications: %s", message)
		}
	}
}

func (w *DeliveryWorker) nextWait() time.Duration {
	w.mu.Lock()
	defer w.mu.Unlock()
	now := time.Now()
	deadline := now.Add(LeaseRenewInterval)
	for _, when := range w.retryAt {
		if when.After(now) && when.Before(deadline) {
			deadline = when
		}
	}
	for _, when := range w.wakeDeadlineAt {
		if when.After(now) && when.Before(deadline) {
			deadline = when
		}
	}
	if remaining := time.Until(deadline); remaining > 0 {
		return remaining
	}
	return 0
}

func (w *DeliveryWorker) reconcile(ctx context.Context) error {
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
	for _, sandbox := range sandboxes {
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
	w.mu.Lock()
	for uid := range w.owners {
		if _, ok := liveUID[uid]; !ok {
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
	active := make(map[string]struct{}, len(openings)+len(heads))
	for _, opening := range openings {
		active["opening:"+opening.SessionID] = struct{}{}
	}
	for _, head := range heads {
		active[head.CommandID] = struct{}{}
	}
	w.mu.Lock()
	for key := range w.retryAt {
		if _, okay := active[key]; !okay {
			delete(w.retryAt, key)
			delete(w.attempts, key)
		}
	}
	for key := range w.wakeDeadlineAt {
		if _, okay := active[key]; !okay {
			delete(w.wakeDeadlineAt, key)
		}
	}
	w.mu.Unlock()
	for _, opening := range openings {
		sandbox, live := byName[opening.SandboxName]
		if !live || sandbox.UID != opening.SandboxUID {
			continue
		}
		owner, okay := w.ownerFor(opening.SandboxUID)
		if !okay || !w.canStart(opening.SessionID) {
			continue
		}
		ready, err := w.prepareOpening(ctx, opening, sandbox, owner)
		if err != nil {
			return err
		}
		if ready {
			w.start(opening.SessionID, func() { w.deliverOpening(ctx, opening, sandbox, owner) })
		}
	}
	for _, head := range heads {
		sandbox, live := byName[head.SandboxName]
		if !live || sandbox.UID != head.SandboxUID {
			continue
		}
		owner, okay := w.ownerFor(head.SandboxUID)
		if !okay || !w.canStart(head.SessionID) {
			continue
		}
		ready, err := w.prepareDispatch(ctx, head, sandbox, owner)
		if err != nil {
			return err
		}
		if ready {
			w.start(head.SessionID, func() { w.deliverCommand(ctx, head, sandbox, owner) })
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

func (w *DeliveryWorker) canStart(sessionID string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, busy := w.delivering[sessionID]; busy {
		return false
	}
	return true
}

func (w *DeliveryWorker) retryPending(key string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	until, retrying := w.retryAt[key]
	if retrying && until.After(time.Now()) {
		return true
	}
	delete(w.retryAt, key)
	return false
}

func (w *DeliveryWorker) start(sessionID string, deliver func()) {
	w.mu.Lock()
	if _, busy := w.delivering[sessionID]; busy {
		w.mu.Unlock()
		return
	}
	w.delivering[sessionID] = struct{}{}
	w.mu.Unlock()
	go func() {
		defer func() {
			w.mu.Lock()
			delete(w.delivering, sessionID)
			w.mu.Unlock()
			select {
			case w.done <- struct{}{}:
			default:
			}
		}()
		deliver()
	}()
}

func (w *DeliveryWorker) clearRetry(key string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.attempts, key)
	delete(w.retryAt, key)
	delete(w.wakeDeadlineAt, key)
}

func (w *DeliveryWorker) backoff(key string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.attempts[key]++
	delay := 500 * time.Millisecond
	for i := 1; i < w.attempts[key] && delay < 30*time.Second; i++ {
		delay *= 2
	}
	if delay > 30*time.Second {
		delay = 30 * time.Second
	}
	w.retryAt[key] = time.Now().Add(delay)
}

func (w *DeliveryWorker) setRetry(key string, after time.Duration) {
	w.mu.Lock()
	w.retryAt[key] = time.Now().Add(after)
	w.mu.Unlock()
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

func (w *DeliveryWorker) deliveryFailure(key, uid, incarnation, operation string, err error) (string, string) {
	grpcStatus, okay := status.FromError(err)
	if !okay {
		w.backoff(key)
		return QueueState, "delivery_unavailable"
	}
	switch grpcStatus.Code() {
	case codes.Aborted:
		w.markRefused(uid, incarnation)
		return QueueState, "runner_refused_work"
	case codes.Unavailable, codes.DeadlineExceeded:
		w.backoff(key)
		return QueueState, "delivery_unavailable"
	case codes.FailedPrecondition:
		if operation == "stop_runner_session" {
			return "NO_OP", "nothing_running"
		}
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

func (w *DeliveryWorker) deliverCommand(ctx context.Context, head *Dispatch, sandbox SandboxSnapshot, owner Owner) {
	claimed, err := w.Store.Settle(ctx, head.CommandID, owner, []string{QueueState}, DeliveringState, "", nil)
	if err != nil {
		w.Logger.Error("cannot claim queued dispatch", "command_id", head.CommandID, "error", err)
		return
	}
	if claimed == nil {
		return
	}
	w.clearRetry(head.CommandID)
	admission, callErr := w.Runner.Deliver(ctx, sandbox, head.SessionID, head.Command)
	state, reason := AdmittedState, ""
	if callErr != nil {
		state, reason = w.deliveryFailure(head.CommandID, head.SandboxUID, sandbox.Incarnation, head.Operation, callErr)
	}
	w.retryStorage(ctx, func() error {
		_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{DeliveringState}, state, reason, admission)
		return err
	})
}

func (w *DeliveryWorker) deliverOpening(ctx context.Context, opening *Opening, sandbox SandboxSnapshot, owner Owner) {
	key := "opening:" + opening.SessionID
	claimed, err := w.Store.SettleOpening(ctx, opening.SessionID, owner, []string{QueueState}, DeliveringState, "")
	if err != nil {
		w.Logger.Error("cannot claim queued opening", "session_id", opening.SessionID, "error", err)
		return
	}
	if claimed == nil {
		return
	}
	w.clearRetry(key)
	callErr := w.Runner.OpenSession(ctx, sandbox, opening.SessionID, opening.Spec, opening.SetupScript)
	state, reason := AdmittedState, ""
	if callErr != nil {
		state, reason = w.deliveryFailure(key, opening.SandboxUID, sandbox.Incarnation, "open_session", callErr)
	}
	w.retryStorage(ctx, func() error {
		_, err := w.Store.SettleOpening(ctx, opening.SessionID, owner, []string{DeliveringState}, state, reason)
		return err
	})
}
