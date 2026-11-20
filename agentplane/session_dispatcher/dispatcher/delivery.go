package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/client-go/util/workqueue"
)

const deliveryWorkerCount = 4

type retryState struct {
	workID string
	until  time.Time
}

// SandboxCatalog must return a complete listing after synchronization. It also
// drives the live sender's name->UID lookup; it does not derive session identity
// from History or a process-local runner list.
type SandboxCatalog interface {
	SandboxReader
	ListSandboxes(context.Context) ([]SandboxSnapshot, error)
	// CurrentSandboxUIDs performs an uncached, complete API read. Call it after
	// collecting stored deletion candidates; cached absence cannot authorize cleanup.
	CurrentSandboxUIDs(context.Context) (map[string]struct{}, error)
	// WatchSandboxes hands each add/update/delete directly to enqueue, including relist deletions.
	WatchSandboxes(enqueue func(SandboxKey)) (func(), error)
	// SandboxExists checks the exact UID against the API server, never cached absence.
	SandboxExists(context.Context, SandboxKey) (bool, error)
	WaitSynced(context.Context) bool
}

// SandboxKey addresses one incarnation; an empty key is reserved for a full resync.
type SandboxKey struct{ Name, UID string }

// WakeRequester shares the lifecycle owner's live authorization, guarded mutation and budget
// path with explicit resume. Dispatcher supplies only the retained effective sender: the
// ServiceAccount or Identity a row is attributed to.
type WakeRequester interface {
	RequestWake(context.Context, *lifecycle.Target, lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error)
}

type SessionDelivery interface {
	SessionCommandDelivery
	CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, store.SessionExecution) error
}

type DeliveryWorker struct {
	Store    *store.Store
	Catalog  SandboxCatalog
	Wake     WakeRequester
	Delivery SessionDelivery
	Hub      *wakeups.Hub
	Logger   *slog.Logger
	// Ready closes after the first complete inventory and pending-work scan.
	// Each queued Session recovers under its own lease before processing its head.
	Ready chan struct{}
	// ownsHub is set only for the dedicated listener created by NewDeliveryWorker.
	// An injected production Hub remains owned by the process that shares it.
	ownsHub bool

	mu      sync.Mutex
	refused map[string]string
	queue   workqueue.TypedRateLimitingInterface[string]
	limiter workqueue.TypedRateLimiter[string]
	// A delayed item can be added early by a database notification. The worker
	// still checks readiness before honoring a persisted wake retry time.
	retries map[string]retryState
}

func (w *DeliveryWorker) init() {
	if w.Logger == nil {
		w.Logger = slog.Default()
	}
	w.refused = map[string]string{}
	w.limiter = workqueue.NewTypedItemExponentialFailureRateLimiter[string](500*time.Millisecond, 30*time.Second)
	w.queue = workqueue.NewTypedRateLimitingQueue(w.limiter)
	w.retries = map[string]retryState{}
}

func (w *DeliveryWorker) Run(ctx context.Context) error {
	if w.ownsHub {
		defer w.Hub.Close()
	}
	if w.Store == nil || w.Catalog == nil || w.Wake == nil || w.Delivery == nil || w.Hub == nil {
		return errors.New("dispatcher delivery dependencies are required")
	}
	ctx, cancel := context.WithCancel(ctx)
	w.init()
	reconciles := workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[SandboxKey]())
	var workers sync.WaitGroup
	defer func() { cancel(); w.queue.ShutDown(); reconciles.ShutDown(); workers.Wait() }()
	generation := w.Hub.Generation()
	changed, unsubscribe, err := w.Hub.Subscribe(store.NotifyChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	stopPayloads, err := w.Hub.SubscribePayload(store.NotifyChannel, func(id string) {
		if id == "" {
			// Pre-keyed replicas still send empty invalidations during a rolling deploy.
			reconciles.Add(SandboxKey{})
		} else {
			w.enqueue(id)
		}
	})
	if err != nil {
		return err
	}
	defer stopPayloads()
	stopSandboxes, err := w.Catalog.WatchSandboxes(func(key SandboxKey) { reconciles.Add(key) })
	if err != nil {
		return err
	}
	defer stopSandboxes()
	if !w.Catalog.WaitSynced(ctx) {
		if err := ctx.Err(); err != nil {
			return err
		}
		return errors.New("Sandbox inventory did not synchronize")
	}
	for range deliveryWorkerCount {
		workers.Go(func() { w.runQueue(ctx) })
	}
	workers.Go(func() {
		var ready sync.Once
		for {
			key, shutdown := reconciles.Get()
			if shutdown {
				return
			}
			func() {
				defer reconciles.Done(key)
				if ctx.Err() != nil {
					return
				}
				var err error
				if key.UID == "" {
					err = w.reconcile(ctx)
				} else {
					err = w.reconcileSandbox(ctx, key)
				}
				if err != nil {
					w.Logger.Warn("dispatcher reconciliation unavailable; retrying", "sandbox_uid", key.UID, "error", err)
					reconciles.AddRateLimited(key)
					return
				}
				reconciles.Forget(key)
				if key.UID == "" && w.Ready != nil {
					ready.Do(func() { close(w.Ready) })
				}
			}()
		}
	})
	// Subscribe before scanning: commits during the scan remain keyed in the queue.
	reconciles.Add(SandboxKey{})
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
			if current := w.Hub.Generation(); current != generation {
				// Loss invalidates the generation; reconnect establishes LISTEN
				// before its nil wakeup requests the recovery snapshot.
				if w.Hub.Connected() {
					generation = current
					reconciles.Add(SandboxKey{})
				}
			}
		}
	}
}

func (w *DeliveryWorker) reconcileSandbox(ctx context.Context, key SandboxKey) error {
	sessions, err := w.Store.SandboxSessions(ctx, key.UID)
	if err != nil {
		return err
	}
	// Freeze the candidate UID before the authoritative API read. A stale informer
	// cannot authorize deletion, including when a same-name replacement is live.
	live, err := w.Catalog.SandboxExists(ctx, key)
	if err != nil {
		return err
	}
	if !live {
		return w.Store.EndSandbox(ctx, key.UID)
	}
	for _, id := range sessions {
		w.enqueue(id)
	}
	return nil
}

func (w *DeliveryWorker) reconcile(ctx context.Context) (result error) {
	ctx, span := observability.Start(ctx, "Dispatcher Reconcile")
	defer func() { observability.End(span, result) }()
	listCtx, listSpan := observability.Start(ctx, "Dispatcher Reconcile Sandbox Inventory")
	sandboxes, err := w.Catalog.ListSandboxes(listCtx)
	observability.End(listSpan, err)
	if err != nil {
		return err
	}
	byName := make(map[string]SandboxSnapshot, len(sandboxes))
	liveUID := make(map[string]struct{}, len(sandboxes))
	for _, sandbox := range sandboxes {
		byName[sandbox.Name] = sandbox
		liveUID[sandbox.UID] = struct{}{}
	}
	listCtx, listSpan = observability.Start(ctx, "Dispatcher Reconcile Stored Sandboxes")
	storedUID, err := w.Store.ListSandboxUIDs(listCtx)
	observability.End(listSpan, err)
	if err != nil {
		return err
	}
	var candidates []string
	for uid := range storedUID {
		if _, live := liveUID[uid]; !live {
			candidates = append(candidates, uid)
		}
	}
	if len(candidates) > 0 {
		// A new environment can appear after the cached scan but before its session
		// reaches PostgreSQL. Confirm absence only after freezing that candidate set.
		currentUIDs, err := w.Catalog.CurrentSandboxUIDs(ctx)
		if err != nil {
			return err
		}
		for _, uid := range candidates {
			if _, live := currentUIDs[uid]; !live {
				if err := w.Store.EndSandbox(ctx, uid); err != nil {
					return err
				}
			}
		}
	}
	// Include in-flight work: it has no runnable head until a successor takes its Session lease.
	sessions, err := w.Store.SessionsWithPendingWork(ctx)
	if err != nil {
		return err
	}
	for _, session := range sessions {
		sandbox, live := byName[session.SandboxName]
		if live && sandbox.UID == session.SandboxUID {
			w.enqueue(session.SessionID)
		}
	}
	return nil
}

func (w *DeliveryWorker) enqueue(sessionID string) {
	retry := w.retryFor(sessionID)
	if delay := time.Until(retry.until); delay > 0 {
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

func (w *DeliveryWorker) processSession(ctx context.Context, sessionID string) (result error) {
	ctx, span := observability.Start(ctx, "Dispatcher Process Session", trace.WithAttributes(attribute.String("session.id", sessionID)))
	defer func() { observability.End(span, result) }()
	stored, err := w.Store.GetSession(ctx, sessionID)
	if errors.Is(err, store.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	sandbox, live, err := w.Catalog.GetSandbox(ctx, stored.SandboxName)
	if err != nil || !live || sandbox.UID != stored.SandboxUID {
		return err
	}
	acquired, err := w.Store.AcquireSessionLease(ctx, sessionID)
	var held *lease.HeldError
	if errors.As(err, &held) {
		w.queue.AddAfter(sessionID, time.Until(held.Until))
		return nil
	}
	if err != nil {
		return err
	}
	owner := *acquired
	ctx, cancel := context.WithCancel(ctx)
	renewed := make(chan struct{})
	go func() {
		defer close(renewed)
		ticker := time.NewTicker(store.LeaseRenewInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := w.Store.RenewSessionLease(ctx, owner); err != nil {
					w.Logger.Warn("Session delivery lease lost", "session_id", sessionID, "error", err)
					cancel()
					return
				}
			}
		}
	}()
	defer func() {
		cancel()
		<-renewed
		releaseCtx, releaseCancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer releaseCancel()
		result = errors.Join(result, w.Store.ReleaseSessionLease(releaseCtx, owner))
	}()
	current, err := w.Store.RecoverSession(ctx, owner)
	if err != nil || !current {
		return err
	}
	session, head, err := w.Store.SessionHead(ctx, sessionID)
	if err != nil {
		return err
	}
	if session == nil && head == nil {
		w.clearRetry(sessionID)
		return nil
	}
	workID := "session:" + sessionID
	if head != nil {
		workID = head.CommandID
	}
	w.mu.Lock()
	if previous := w.retries[sessionID].workID; previous != "" && previous != workID {
		delete(w.retries, sessionID)
		w.queue.Forget(sessionID)
	}
	w.mu.Unlock()
	queuedSession := session
	principalSession := session
	if principalSession == nil {
		principalSession, err = w.Store.GetSession(ctx, sessionID)
		if errors.Is(err, store.ErrNotFound) {
			principalSession = nil
		} else if err != nil {
			return err
		}
	}
	// Attributed dispatch rows without a Session are retained as historical receipts, not executable work.
	if principalSession == nil {
		return nil
	}
	if head != nil && (principalSession.State == store.SessionFailedState || principalSession.State == store.SessionUnknownState) {
		return w.Store.NoteWaiting(ctx, head.CommandID, owner, "session_unconfirmed")
	}
	identity, found, err := w.executionIdentity(ctx, sandbox)
	if err != nil {
		return err
	}
	if !found || !store.SameIdentity(identity, principalSession.ExecutionIdentity) {
		return w.failExecutionIdentity(ctx, principalSession, head, owner)
	}
	if queuedSession != nil {
		ready, err := w.prepareSession(ctx, queuedSession, sandbox, owner)
		if err != nil || !ready {
			return err
		}
		if w.deliverSession(ctx, queuedSession, sandbox, owner) {
			w.enqueue(sessionID)
		}
	} else {
		ready, err := w.prepareDispatch(ctx, head, sandbox, owner)
		if err != nil || !ready {
			return err
		}
		if w.deliverCommand(ctx, head, sandbox, owner, principalSession) {
			w.enqueue(sessionID)
		}
	}
	return nil
}

func (w *DeliveryWorker) executionIdentity(ctx context.Context, sandbox SandboxSnapshot) (*identitypb.Identity, bool, error) {
	reader, ok := w.Catalog.(ExecutionIdentityReader)
	if !ok {
		return nil, false, errors.New("Sandbox execution Identity reader unavailable")
	}
	return reader.GetExecutionIdentity(ctx, sandbox)
}

// executionCurrent reports whether the Sandbox still names the exact Identity incarnation recorded
// for the Session.
func (w *DeliveryWorker) executionCurrent(ctx context.Context, session *store.Session, sandbox SandboxSnapshot) (bool, error) {
	identity, found, err := w.executionIdentity(ctx, sandbox)
	if err != nil {
		return false, err
	}
	return found && store.SameIdentity(identity, session.ExecutionIdentity), nil
}

func (w *DeliveryWorker) failExecutionIdentity(ctx context.Context, session *store.Session, head *store.Dispatch, owner store.SessionOwner) error {
	if head == nil {
		_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{store.SessionWaitingState}, store.SessionFailedState, "execution_identity_changed")
		if err == nil {
			w.clearRetry(session.SessionID)
		}
		return err
	}
	_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{store.QueueState}, store.FailedState, "execution_identity_changed", nil)
	if err == nil {
		w.clearRetry(head.SessionID)
	}
	return err
}

func (w *DeliveryWorker) clearRetry(sessionID string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.retries, sessionID)
	w.queue.Forget(sessionID)
}

func (w *DeliveryWorker) backoff(sessionID, workID string) {
	delay := w.limiter.When(sessionID)
	w.setRetry(sessionID, workID, delay)
}

func (w *DeliveryWorker) setRetry(sessionID, workID string, after time.Duration) {
	w.mu.Lock()
	w.retries[sessionID] = retryState{workID: workID, until: time.Now().Add(after)}
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
		return store.QueueState, "runner_unreachable: " + err.Error()
	}
	switch grpcStatus.Code() {
	case codes.Aborted:
		w.markRefused(uid, incarnation)
		return store.QueueState, "runner_refused_work"
	case codes.Unavailable, codes.DeadlineExceeded:
		w.backoff(sessionID, workID)
		return store.QueueState, fmt.Sprintf("runner_unreachable: %s: %s", grpcStatus.Code(), grpcStatus.Message())
	}
	return store.FailedState, fmt.Sprintf("runner %s: %s", grpcStatus.Code(), grpcStatus.Message())
}

// Both placement owners validate arguments before reservation and report AlreadyExists for a
// conflicting retained request. Other terminal codes do not prove rejection.
func (w *DeliveryWorker) sessionFailure(session *store.Session, sandbox SandboxSnapshot, err error) (string, string, *status.Status) {
	key := "session:" + session.SessionID
	switch status.Code(err) {
	case codes.InvalidArgument, codes.AlreadyExists:
		rejection := status.Convert(err)
		return store.SessionFailedState, fmt.Sprintf("runner %s: %s", rejection.Code(), rejection.Message()), rejection
	case codes.Aborted, codes.Unavailable, codes.DeadlineExceeded:
		// Reconcile the same Runtime reservation; native preparation waits for work.
		state, reason := w.deliveryFailure(session.SessionID, key, session.SandboxUID, sandbox.Incarnation, err)
		return sessionMaterializationFromDelivery(state), reason, nil
	default:
		return store.SessionUnknownState, "session_materialization_unconfirmed: " + err.Error(), nil
	}
}

func sessionMaterializationFromDelivery(state string) string {
	switch state {
	case store.QueueState:
		return store.SessionWaitingState
	case store.FailedState:
		return store.SessionFailedState
	default:
		panic("unexpected Session delivery failure state: " + state)
	}
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

func (w *DeliveryWorker) deliverCommand(ctx context.Context, head *store.Dispatch, sandbox SandboxSnapshot, owner store.SessionOwner, session *store.Session) bool {
	if session == nil {
		return false
	}
	ctx = observability.WithTraceparent(ctx, head.Traceparent)
	ctx, span := observability.Start(
		ctx, "Dispatcher Deliver Command",
		trace.WithAttributes(
			attribute.String("session.id", head.SessionID),
			attribute.String("sandbox.name", head.SandboxName),
		),
	)
	defer span.End()
	claimed, err := w.Store.Settle(ctx, head.CommandID, owner, []string{store.QueueState}, store.DeliveringState, "", nil)
	if err != nil {
		span.SetStatus(otelcodes.Error, "dispatch claim failed")
		w.Logger.Error("cannot claim queued dispatch", "command_id", head.CommandID, "error", err)
		w.backoff(head.SessionID, head.CommandID)
		return false
	}
	if claimed == nil {
		return false
	}
	if session != nil {
		current, identityErr := w.executionCurrent(ctx, session, sandbox)
		if identityErr != nil {
			w.retryStorage(ctx, func() error {
				_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{store.DeliveringState}, store.QueueState, "execution_identity_unavailable", nil)
				return err
			})
			w.backoff(head.SessionID, head.CommandID)
			return true
		}
		if !current {
			w.retryStorage(ctx, func() error {
				state := store.FailedState
				if head.DeliveryAttempted {
					state = store.UnknownState
				}
				_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{store.DeliveringState}, state, "execution_identity_changed", nil)
				return err
			})
			w.clearRetry(head.SessionID)
			return true
		}
	}
	var marked bool
	w.retryStorage(ctx, func() error {
		var err error
		marked, err = w.Store.MarkDeliveryAttempt(ctx, head.CommandID, owner)
		return err
	})
	if !marked || ctx.Err() != nil {
		return false
	}
	admission, callErr := w.Delivery.Deliver(ctx, sandbox, head.SessionID, head.Command)
	if callErr != nil {
		span.SetStatus(otelcodes.Error, "runner delivery failed")
	}
	state, reason := store.AdmittedState, ""
	if callErr != nil {
		state, reason = w.deliveryFailure(head.SessionID, head.CommandID, head.SandboxUID, sandbox.Incarnation, callErr)
		// A bare RPC error proves neither non-admission nor the outcome of an earlier attempt.
		if state == store.FailedState {
			state = store.UnknownState
		}
	}
	w.retryStorage(ctx, func() error {
		_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{store.DeliveringState}, state, reason, admission)
		return err
	})
	if state != store.QueueState {
		w.clearRetry(head.SessionID)
	}
	return true
}

func (w *DeliveryWorker) deliverSession(ctx context.Context, session *store.Session, sandbox SandboxSnapshot, owner store.SessionOwner) bool {
	ctx = observability.WithTraceparent(ctx, session.Traceparent)
	ctx, span := observability.Start(
		ctx, "Dispatcher Deliver Session",
		trace.WithAttributes(
			attribute.String("session.id", session.SessionID),
			attribute.String("sandbox.name", session.SandboxName),
		),
	)
	defer span.End()
	key := "session:" + session.SessionID
	claimed, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{store.SessionWaitingState}, store.SessionInProgressState, "")
	if err != nil {
		span.SetStatus(otelcodes.Error, "Session claim failed")
		w.Logger.Error("cannot claim queued session", "session_id", session.SessionID, "error", err)
		w.backoff(session.SessionID, "session:"+session.SessionID)
		return false
	}
	if claimed == nil {
		return false
	}
	current, identityErr := w.executionCurrent(ctx, session, sandbox)
	if identityErr != nil {
		w.retryStorage(ctx, func() error {
			_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{store.SessionInProgressState}, store.SessionWaitingState, "execution_identity_unavailable")
			return err
		})
		w.backoff(session.SessionID, key)
		return true
	}
	if !current {
		w.retryStorage(ctx, func() error {
			_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{store.SessionInProgressState}, store.SessionFailedState, "execution_identity_changed")
			return err
		})
		w.clearRetry(session.SessionID)
		return true
	}
	callErr := w.Delivery.CreateSession(ctx, sandbox, session.SessionID, session.Spec, session.SetupScript, session.Execution())
	if callErr != nil {
		span.SetStatus(otelcodes.Error, "runner Session failed")
	}
	state, reason := store.SessionAcceptedState, ""
	var rejection *status.Status
	if callErr != nil {
		state, reason, rejection = w.sessionFailure(session, sandbox, callErr)
	}
	w.retryStorage(ctx, func() error {
		if rejection != nil {
			_, err := w.Store.RejectSession(ctx, session.SessionID, owner, []string{store.SessionInProgressState}, reason)
			return err
		}
		_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{store.SessionInProgressState}, state, reason)
		return err
	})
	if state != store.SessionWaitingState {
		w.clearRetry(session.SessionID)
	}
	return true
}
