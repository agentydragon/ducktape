package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
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
	Changes() <-chan struct{}
	WaitSynced(context.Context) bool
}

// WakeRequester shares the lifecycle owner's live authorization, guarded mutation and budget
// path with explicit resume. Dispatcher supplies only the retained effective sender identity.
type WakeRequester interface {
	RequestWake(context.Context, *lifecycle.Target, *lifecycle.ServiceAccountIdentity) (*environmentpb.WakeAssessment, error)
}

type DeliveryRunner interface {
	RunnerCommands
	CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, SessionExecution) error
}

type LeaseOwner interface {
	Acquire(context.Context, string, string) (*Owner, error)
	StillOwns(context.Context, Owner) (bool, error)
}

type DeliveryWorker struct {
	Store   *Store
	Catalog SandboxCatalog
	Wake    WakeRequester
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
	// A delayed item can be added early by a database notification. The worker
	// still checks readiness before honoring a persisted wake retry time.
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
	// A queue key is a Session ID, so even with multiple workers a Session
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
	ctx, span := observability.Start(ctx, "Dispatcher Reconcile", trace.WithAttributes(attribute.Bool("lease.renew", renew)))
	defer func() { observability.End(span, result) }()
	// A partial scan or failed renewal cannot leave an old token available to
	// independently running queue workers. The next successful renewal restores it.
	defer func() {
		if result != nil {
			w.mu.Lock()
			clear(w.owners)
			w.mu.Unlock()
		}
	}()
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
				if _, err := w.Store.EndSandbox(ctx, uid); err != nil {
					return err
				}
			}
		}
	}
	listCtx, listSpan = observability.Start(ctx, "Dispatcher Reconcile Pending Work")
	pending, err := w.Store.SandboxesWithPendingWork(listCtx)
	observability.End(listSpan, err)
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
		leaseCtx, leaseSpan := observability.Start(ctx, "Dispatcher Reconcile Acquire Lease", trace.WithAttributes(attribute.String("sandbox.name", sandbox.Name)))
		owner, err := w.Leases.Acquire(leaseCtx, sandbox.Name, sandbox.UID)
		observability.End(leaseSpan, err)
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
			recoveryCtx, recoverySpan := observability.Start(ctx, "Dispatcher Reconcile Recover Sandbox", trace.WithAttributes(attribute.String("sandbox.name", sandbox.Name)))
			current, err := w.Store.RecoverSandbox(recoveryCtx, *owner)
			observability.End(recoverySpan, err)
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
	listCtx, listSpan = observability.Start(ctx, "Dispatcher Reconcile Session Heads")
	sessions, err := w.Store.SessionHeads(listCtx)
	observability.End(listSpan, err)
	if err != nil {
		return err
	}
	listCtx, listSpan = observability.Start(ctx, "Dispatcher Reconcile Command Heads")
	heads, err := w.Store.Heads(listCtx)
	observability.End(listSpan, err)
	if err != nil {
		return err
	}
	for _, session := range sessions {
		sandbox, live := byName[session.SandboxName]
		if live && sandbox.UID == session.SandboxUID {
			w.enqueue(session.SessionID)
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
	session, head, err := w.Store.SessionHead(ctx, sessionID)
	if err != nil {
		return err
	}
	if session == nil && head == nil {
		w.clearRetry(sessionID)
		return nil
	}
	name, uid, workID := "", "", ""
	if session != nil {
		name, uid, workID = session.SandboxName, session.SandboxUID, "session:"+session.SessionID
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
	queuedSession := session
	principalSession := session
	if principalSession == nil {
		principalSession, err = w.Store.GetSession(ctx, sessionID)
		if errors.Is(err, ErrNotFound) {
			principalSession = nil
		} else if err != nil {
			return err
		}
	}
	if head != nil && principalSession != nil && principalSession.Placement.requiresReadyExecutor() &&
		(principalSession.State == SessionFailedState || principalSession.State == SessionUnknownState) {
		return w.Store.NoteWaiting(ctx, head.CommandID, owner, "session_unconfirmed")
	}
	if principalSession != nil && principalSession.ExecutionPrincipal != nil {
		account, found, err := w.executionAccount(ctx, sandbox)
		if err != nil {
			return err
		}
		expected := principalSession.ExecutionPrincipal
		if !found && expected.GetUid() == "" && head == nil {
			return w.Store.NoteSessionWaiting(ctx, principalSession.SessionID, owner, "execution_identity_pending")
		}
		if !found || account == nil || accountOfIdentity(account) != accountOfIdentity(expected) ||
			accountOfIdentity(expected) != (Account{Namespace: sandbox.Namespace, Name: sandbox.Name}) {
			return w.failExecutionIdentity(ctx, principalSession, head, owner)
		}
		if expected.GetUid() == "" && head == nil {
			principalSession, err = w.Store.BindExecutionPrincipal(ctx, principalSession.SessionID, principalSession.SandboxUID,
				accountOfIdentity(expected).Namespace, accountOfIdentity(expected).Name, account.GetUid())
			if err != nil {
				return err
			}
			expected = principalSession.ExecutionPrincipal
			queuedSession = principalSession
		}
		if account.GetUid() != expected.GetUid() || expected.GetUid() == "" {
			return w.failExecutionIdentity(ctx, principalSession, head, owner)
		}
		identity, found, err := w.executionIdentity(ctx, sandbox)
		if err != nil {
			return err
		}
		if !found || !sameIdentity(identity, principalSession.ExecutionIdentity) {
			return w.failExecutionIdentity(ctx, principalSession, head, owner)
		}
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

func (w *DeliveryWorker) executionAccount(ctx context.Context, sandbox SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	reader, ok := w.Catalog.(ExecutionAccountReader)
	if !ok {
		return nil, false, errors.New("Sandbox execution account reader unavailable")
	}
	return reader.GetExecutionAccount(ctx, sandbox)
}

func (w *DeliveryWorker) executionIdentity(ctx context.Context, sandbox SandboxSnapshot) (*identitypb.Identity, bool, error) {
	reader, ok := w.Catalog.(ExecutionAccountReader)
	if !ok {
		return nil, false, errors.New("Sandbox execution account reader unavailable")
	}
	return reader.GetExecutionIdentity(ctx, sandbox)
}

// executionCurrent reports whether the Sandbox still runs as the exact ServiceAccount and Identity
// session recorded: an Identity changed while the Sandbox was stopped strands the Session rather
// than re-attributing it.
func (w *DeliveryWorker) executionCurrent(ctx context.Context, session *Session, sandbox SandboxSnapshot) (bool, error) {
	account, found, err := w.executionAccount(ctx, sandbox)
	if err != nil {
		return false, err
	}
	if !found || account == nil || accountOfIdentity(account) != accountOfIdentity(session.ExecutionPrincipal) ||
		session.ExecutionPrincipal.GetUid() == "" || account.GetUid() != session.ExecutionPrincipal.GetUid() {
		return false, nil
	}
	identity, found, err := w.executionIdentity(ctx, sandbox)
	if err != nil {
		return false, err
	}
	return found && sameIdentity(identity, session.ExecutionIdentity), nil
}

func (w *DeliveryWorker) failExecutionIdentity(ctx context.Context, session *Session, head *Dispatch, owner Owner) error {
	if head == nil {
		_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{SessionWaitingState}, SessionFailedState, "execution_identity_changed")
		if err == nil {
			w.clearRetry(session.SessionID)
		}
		return err
	}
	_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{QueueState}, FailedState, "execution_identity_changed", nil)
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

// Runtime validates arguments before reservation and reports AlreadyExists for a
// conflicting retained request. Other terminal codes do not prove native rejection.
func (w *DeliveryWorker) sessionFailure(session *Session, sandbox SandboxSnapshot, err error) (string, string, *status.Status) {
	key := "session:" + session.SessionID
	if session.Placement == CentralSampler {
		state, reason := w.deliveryFailure(session.SessionID, key, session.SandboxUID, sandbox.Incarnation, err)
		return sessionMaterializationFromDelivery(state), reason, nil
	}
	switch status.Code(err) {
	case codes.InvalidArgument, codes.AlreadyExists:
		rejection := status.Convert(err)
		return SessionFailedState, fmt.Sprintf("runner %s: %s", rejection.Code(), rejection.Message()), rejection
	case codes.Aborted, codes.Unavailable, codes.DeadlineExceeded:
		// Reconcile the same Runtime reservation; native preparation waits for work.
		state, reason := w.deliveryFailure(session.SessionID, key, session.SandboxUID, sandbox.Incarnation, err)
		return sessionMaterializationFromDelivery(state), reason, nil
	default:
		return SessionUnknownState, "session_materialization_unconfirmed: " + err.Error(), nil
	}
}

func sessionMaterializationFromDelivery(state string) string {
	switch state {
	case QueueState:
		return SessionWaitingState
	case FailedState:
		return SessionFailedState
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

func (w *DeliveryWorker) deliverCommand(ctx context.Context, head *Dispatch, sandbox SandboxSnapshot, owner Owner, session *Session) bool {
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
	if session != nil && session.ExecutionPrincipal != nil {
		current, identityErr := w.executionCurrent(ctx, session, sandbox)
		if identityErr != nil {
			w.retryStorage(ctx, func() error {
				_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{DeliveringState}, QueueState, "execution_identity_unavailable", nil)
				return err
			})
			w.backoff(head.SessionID, head.CommandID)
			return true
		}
		if !current {
			w.retryStorage(ctx, func() error {
				state := FailedState
				if head.DeliveryAttempted {
					state = UnknownState
				}
				_, err := w.Store.Settle(ctx, head.CommandID, owner, []string{DeliveringState}, state, "execution_identity_changed", nil)
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
	admission, callErr := w.Runner.Deliver(ctx, sandbox, head.SessionID, head.Command)
	if callErr != nil {
		span.SetStatus(otelcodes.Error, "runner delivery failed")
	}
	state, reason := AdmittedState, ""
	if callErr != nil {
		state, reason = w.deliveryFailure(head.SessionID, head.CommandID, head.SandboxUID, sandbox.Incarnation, callErr)
		// A bare RPC error proves neither non-admission nor the outcome of an earlier attempt.
		if state == FailedState {
			state = UnknownState
		}
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

func (w *DeliveryWorker) deliverSession(ctx context.Context, session *Session, sandbox SandboxSnapshot, owner Owner) bool {
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
	claimed, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{SessionWaitingState}, SessionInProgressState, "")
	if err != nil {
		span.SetStatus(otelcodes.Error, "Session claim failed")
		w.Logger.Error("cannot claim queued session", "session_id", session.SessionID, "error", err)
		return false
	}
	if claimed == nil {
		return false
	}
	if session.ExecutionPrincipal != nil {
		current, identityErr := w.executionCurrent(ctx, session, sandbox)
		if identityErr != nil {
			w.retryStorage(ctx, func() error {
				_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{SessionInProgressState}, SessionWaitingState, "execution_identity_unavailable")
				return err
			})
			w.backoff(session.SessionID, key)
			return true
		}
		if !current {
			w.retryStorage(ctx, func() error {
				_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{SessionInProgressState}, SessionFailedState, "execution_identity_changed")
				return err
			})
			w.clearRetry(session.SessionID)
			return true
		}
	}
	callErr := w.Runner.CreateSession(ctx, sandbox, session.SessionID, session.Spec, session.SetupScript, session.execution())
	if callErr != nil {
		span.SetStatus(otelcodes.Error, "runner Session failed")
	}
	state, reason := SessionAcceptedState, ""
	var rejection *status.Status
	if callErr != nil {
		state, reason, rejection = w.sessionFailure(session, sandbox, callErr)
	}
	w.retryStorage(ctx, func() error {
		if rejection != nil {
			_, err := w.Store.RejectSession(ctx, session.SessionID, owner, []string{SessionInProgressState}, reason)
			return err
		}
		_, err := w.Store.SettleSession(ctx, session.SessionID, owner, []string{SessionInProgressState}, state, reason)
		return err
	})
	if state != SessionWaitingState {
		w.clearRetry(session.SessionID)
	}
	return true
}
