package dispatcher

import (
	"context"
	"errors"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type queuedWork struct {
	id, key         string
	session         bool
	attribution     Attribution
	wakeFrom        *string
	wakeRequestedAt *time.Time
	wakeAssessment  *environmentpb.WakeAssessment
	otherWake       *environmentpb.WakeAssessment
}

type wakeBarrier struct {
	id         string
	session    bool
	assessment *environmentpb.WakeAssessment
}

func pendingWake(session *Session, commands []*Dispatch, generation int64) *wakeBarrier {
	if session != nil && session.State == SessionWaitingState && wakeBlocksOther(session.WakeAssessment, generation) {
		return &wakeBarrier{id: session.SessionID, session: true, assessment: session.WakeAssessment}
	}
	for _, command := range commands {
		if wakeBlocksOther(command.WakeAssessment, generation) {
			return &wakeBarrier{id: command.CommandID, assessment: command.WakeAssessment}
		}
	}
	return nil
}

func (barrier *wakeBarrier) other(id string, session bool) *environmentpb.WakeAssessment {
	if barrier == nil || (barrier.id == id && barrier.session == session) {
		return nil
	}
	return barrier.assessment
}

func (w *DeliveryWorker) prepareDispatch(ctx context.Context, row *Dispatch, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	session, err := w.Store.GetSession(ctx, row.SessionID)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return false, err
	}
	var commands []*Dispatch
	if session != nil && session.State == SessionWaitingState && session.Placement.requiresReadyExecutor() &&
		sandbox.Blocker == "sandbox_suspended" && sandbox.WakeOnSend && !sandbox.ManualStop {
		commands, err = w.Store.WakeCommands(ctx, row.SessionID, sandbox.Name, sandbox.UID)
		if err != nil {
			return false, err
		}
	}
	return w.prepareCommand(ctx, row, session, sandbox, owner, pendingWake(session, commands, sandbox.WakeGeneration))
}

func (w *DeliveryWorker) prepareCommand(ctx context.Context, row *Dispatch, session *Session, sandbox SandboxSnapshot, owner Owner, barrier *wakeBarrier) (bool, error) {
	work := queuedWork{
		id: row.CommandID, key: row.SessionID, attribution: row.Attribution,
		wakeFrom: row.WakeFromIncarnation, wakeRequestedAt: row.WakeRequestedAt, wakeAssessment: row.WakeAssessment,
		otherWake: barrier.other(row.CommandID, false),
	}
	if session != nil && !session.Placement.requiresReadyExecutor() {
		return w.prepareCentral(ctx, work, sandbox, owner)
	}
	return w.prepare(ctx, work, sandbox, owner)
}

func (w *DeliveryWorker) prepareSession(ctx context.Context, row *Session, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	work := queuedWork{
		id: row.SessionID, key: row.SessionID, session: true,
		attribution: row.Attribution,
		wakeFrom:    row.WakeFromIncarnation, wakeRequestedAt: row.WakeRequestedAt, wakeAssessment: row.WakeAssessment,
	}
	if !row.Placement.requiresReadyExecutor() {
		return w.prepareCentral(ctx, work, sandbox, owner)
	}
	assessDownstream := sandbox.Blocker == "sandbox_suspended" && sandbox.WakeOnSend && !sandbox.ManualStop
	var commands []*Dispatch
	if assessDownstream {
		var err error
		commands, err = w.Store.WakeCommands(ctx, row.SessionID, sandbox.Name, sandbox.UID)
		if err != nil {
			return false, err
		}
	}
	barrier := pendingWake(row, commands, sandbox.WakeGeneration)
	work.otherWake = barrier.other(row.SessionID, true)
	ready, err := w.prepare(ctx, work, sandbox, owner)
	if err != nil || ready || !assessDownstream {
		return ready, err
	}
	current, err := w.Store.GetSession(ctx, row.SessionID)
	if err != nil || current.State != SessionWaitingState || !callerWakeDenied(current.WakeAssessment) {
		return false, err
	}
	if barrier != nil {
		// Cancellation does not relinquish a possibly in-flight wake.
		for _, command := range commands {
			if !barrier.session && command.CommandID == barrier.id && command.State == QueueState {
				_, err := w.prepareCommand(ctx, command, current, sandbox, owner, barrier)
				return false, err
			}
		}
		w.scheduleWakeRetry(row.SessionID, barrier.assessment)
		return false, nil
	}
	for _, command := range commands {
		if command.State != QueueState || command.Attribution == nil {
			continue
		}
		if _, err := w.prepareCommand(ctx, command, current, sandbox, owner, nil); err != nil {
			return false, err
		}
		result, err := w.Store.GetDispatch(ctx, command.CommandID)
		if err != nil || !callerWakeDenied(result.WakeAssessment) {
			return false, err
		}
	}
	return false, nil
}

func callerWakeDenied(assessment *environmentpb.WakeAssessment) bool {
	return assessment.GetState() == environmentpb.WakeState_WAKE_STATE_DENIED &&
		assessment.GetReason() == environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED
}

func wakeBlocksOther(assessment *environmentpb.WakeAssessment, generation int64) bool {
	switch assessment.GetState() {
	case environmentpb.WakeState_WAKE_STATE_REQUESTED:
		return true
	case environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN:
		return assessment.TargetGeneration == nil || generation <= assessment.GetTargetGeneration()
	case environmentpb.WakeState_WAKE_STATE_RATE_LIMITED:
		return assessment.GetRetryAt() == nil || time.Now().Before(assessment.GetRetryAt().AsTime())
	}
	return false
}

func (w *DeliveryWorker) scheduleWakeRetry(key string, assessment *environmentpb.WakeAssessment) {
	if assessment.GetState() == environmentpb.WakeState_WAKE_STATE_RATE_LIMITED && assessment.GetRetryAt() != nil {
		if delay := time.Until(assessment.GetRetryAt().AsTime()); delay > 0 {
			w.queue.AddAfter(key, delay)
		}
	}
}

func (w *DeliveryWorker) prepareCentral(ctx context.Context, work queuedWork, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	assessment := assessmentFor(sandbox, work.wakeAssessment, environmentpb.WakeState_WAKE_STATE_NOT_NEEDED, 0, nil)
	if err := w.recordAssessment(ctx, work, owner, nil, assessment); err != nil {
		return false, err
	}
	if err := w.Store.ClearWakeState(ctx, work.id, owner, work.session); err != nil {
		return false, err
	}
	retry := w.retryFor(work.key)
	if delay := time.Until(retry.until); delay > 0 {
		w.queue.AddAfter(work.key, delay)
		return false, nil
	}
	return true, nil
}

func (work queuedWork) workID() string {
	if work.session {
		return "session:" + work.id
	}
	return work.id
}

func (w *DeliveryWorker) noteWaiting(ctx context.Context, work queuedWork, owner Owner, reason string) error {
	if work.session {
		return w.Store.NoteSessionWaiting(ctx, work.id, owner, reason)
	}
	return w.Store.NoteWaiting(ctx, work.id, owner, reason)
}

func (w *DeliveryWorker) prepare(ctx context.Context, work queuedWork, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	if sandbox.Deleting {
		return false, w.noteWaiting(ctx, work, owner, "sandbox_deleted")
	}
	if sandbox.ManualStop && sandbox.Blocker == "sandbox_suspended" {
		assessment := assessmentFor(sandbox, work.wakeAssessment,
			environmentpb.WakeState_WAKE_STATE_DENIED,
			environmentpb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP, nil)
		blocker := &environmentpb.DeliveryBlocker{Kind: environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_OPERATOR_REQUESTED_STOP}
		if err := w.recordAssessment(ctx, work, owner, blocker, assessment); err != nil {
			return false, err
		}
		if err := w.Store.ClearWakeState(ctx, work.id, owner, work.session); err != nil {
			return false, err
		}
		return false, w.noteWaiting(ctx, work, owner, "sandbox_operator_requested_stop")
	}
	if sandbox.Blocker == "" {
		assessment := assessmentFor(sandbox, work.wakeAssessment, environmentpb.WakeState_WAKE_STATE_NOT_NEEDED, 0, nil)
		if err := w.recordAssessment(ctx, work, owner, nil, assessment); err != nil {
			return false, err
		}
		if err := w.Store.ClearWakeState(ctx, work.id, owner, work.session); err != nil {
			return false, err
		}
		if w.refusedHere(sandbox.UID, sandbox.Incarnation) {
			return false, w.noteWaiting(ctx, work, owner, "runner_refused_work")
		}
		if delay := time.Until(w.retryFor(work.key).until); delay > 0 {
			w.queue.AddAfter(work.key, delay)
			return false, nil
		}
		return true, nil
	}
	if work.wakeRequestedAt != nil || work.wakeAssessment.GetState() == environmentpb.WakeState_WAKE_STATE_REQUESTED {
		return w.waitForRequestedWake(ctx, work, owner, sandbox, nil)
	}
	if sandbox.Blocker != "sandbox_suspended" {
		if err := w.recordAssessment(ctx, work, owner, nil, work.wakeAssessment); err != nil {
			return false, err
		}
		return false, w.noteWaiting(ctx, work, owner, sandbox.Blocker)
	}

	blocker := &environmentpb.DeliveryBlocker{Kind: environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED}
	if !sandbox.WakeOnSend {
		assessment := assessmentFor(sandbox, work.wakeAssessment,
			environmentpb.WakeState_WAKE_STATE_AUTOMATIC_WAKE_DISABLED, 0, nil)
		if err := w.recordAssessment(ctx, work, owner, blocker, assessment); err != nil {
			return false, err
		}
		return false, w.noteWaiting(ctx, work, owner, "automatic_wake_disabled")
	}

	if work.wakeAssessment.GetState() == environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN &&
		(work.wakeAssessment.TargetGeneration == nil || work.wakeAssessment.GetTargetGeneration() == sandbox.WakeGeneration) {
		// An uncertain patch reply is durable. Reassess only after the observed Sandbox generation
		// changes; this leaves the same uncertain generation untouched for explicit reconciliation.
		if err := w.recordAssessment(ctx, work, owner, blocker, work.wakeAssessment); err != nil {
			return false, err
		}
		return false, w.noteWaiting(ctx, work, owner, "sandbox_wake_outcome_unknown")
	}

	if work.wakeAssessment.GetState() == environmentpb.WakeState_WAKE_STATE_RATE_LIMITED &&
		work.wakeAssessment.GetRetryAt() != nil {
		if delay := time.Until(work.wakeAssessment.GetRetryAt().AsTime()); delay > 0 {
			w.queue.AddAfter(work.key, delay)
			if err := w.recordAssessment(ctx, work, owner, blocker, work.wakeAssessment); err != nil {
				return false, err
			}
			return false, w.noteWaiting(ctx, work, owner, "wake_rate_limited")
		}
	}

	if work.wakeRequestedAt != nil || work.wakeAssessment.GetState() == environmentpb.WakeState_WAKE_STATE_REQUESTED {
		return w.waitForRequestedWake(ctx, work, owner, sandbox, blocker)
	}

	// The retained sender wakes as itself: its effective ServiceAccount, or the Identity incarnation
	// its workload credential named. An unattributed row never wakes.
	var caller lifecycle.Caller
	switch attribution := work.attribution.(type) {
	case *AdmissionAttribution:
		caller = lifecycle.ServiceAccountCaller{Account: attribution.EffectiveCaller}
	case *IdentityAttribution:
		caller = lifecycle.IdentityCaller{Identity: attribution.Identity}
	}
	if caller == nil {
		assessment := assessmentFor(sandbox, work.wakeAssessment,
			environmentpb.WakeState_WAKE_STATE_DENIED,
			environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, nil)
		if err := w.recordAssessment(ctx, work, owner, blocker, assessment); err != nil {
			return false, err
		}
		return false, w.noteWaiting(ctx, work, owner, "sandbox_suspended")
	}
	if work.otherWake != nil {
		w.scheduleWakeRetry(work.key, work.otherWake)
		return false, w.noteWaiting(ctx, work, owner, "session_wake_pending")
	}
	if w.Wake == nil {
		return false, errors.New("lifecycle wake service is unavailable")
	}
	if stillOwns, err := w.Leases.StillOwns(ctx, owner); err != nil || !stillOwns {
		return false, err
	}
	uncertain := assessmentFor(sandbox, nil, environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN,
		environmentpb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN, nil)
	stillQueued, err := w.Store.NoteWaking(ctx, work.id, owner, optionalIncarnation(sandbox.Incarnation), work.session, uncertain)
	if err != nil || !stillQueued {
		return false, err
	}
	stillOwns, err := w.Leases.StillOwns(ctx, owner)
	if err != nil || !stillOwns {
		return false, err
	}
	assessment, err := w.Wake.RequestWake(ctx, &lifecycle.Target{
		Namespace: sandbox.Namespace, Name: sandbox.Name, Uid: sandbox.UID,
	}, caller)
	if err != nil {
		assessment = assessmentFor(sandbox, nil, environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN,
			environmentpb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN, status.Convert(err).Proto())
	}
	if assessment == nil {
		assessment = assessmentFor(sandbox, nil, environmentpb.WakeState_WAKE_STATE_FAILED,
			environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE,
			status.New(codes.Unavailable, "lifecycle returned no wake assessment").Proto())
	}
	assessment = completeAssessmentObservation(sandbox, assessment)
	assessment = preserveObservationTime(work.wakeAssessment, assessment)

	if assessment.GetState() == environmentpb.WakeState_WAKE_STATE_REQUESTED {
		requestedAt := assessment.GetObservedAt().AsTime()
		storedAt, err := w.Store.NoteWakeRequestedAt(ctx, work.id, owner, work.session, requestedAt)
		if err != nil {
			return false, err
		}
		if storedAt == nil {
			return false, nil
		}
		work.wakeRequestedAt = storedAt
	}

	if err := w.recordAssessment(ctx, work, owner, blocker, assessment); err != nil {
		return false, err
	}
	switch assessment.GetState() {
	case environmentpb.WakeState_WAKE_STATE_REQUESTED:
		w.scheduleWakeDeadline(work.key, *work.wakeRequestedAt)
		return false, w.noteWaiting(ctx, work, owner, "sandbox_resuming")
	case environmentpb.WakeState_WAKE_STATE_RATE_LIMITED:
		if assessment.GetRetryAt() != nil {
			remaining := time.Until(assessment.GetRetryAt().AsTime())
			if remaining > 0 {
				w.queue.AddAfter(work.key, remaining)
			}
		}
		return false, w.noteWaiting(ctx, work, owner, "wake_rate_limited")
	case environmentpb.WakeState_WAKE_STATE_FAILED:
		w.backoff(work.key, work.workID())
		return false, w.noteWaiting(ctx, work, owner, "sandbox_wake_failed")
	case environmentpb.WakeState_WAKE_STATE_NOT_NEEDED:
		return false, w.noteWaiting(ctx, work, owner, "sandbox_not_ready")
	case environmentpb.WakeState_WAKE_STATE_AUTOMATIC_WAKE_DISABLED:
		return false, w.noteWaiting(ctx, work, owner, "automatic_wake_disabled")
	case environmentpb.WakeState_WAKE_STATE_DENIED:
		return false, w.noteWaiting(ctx, work, owner, "sandbox_suspended")
	case environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN:
		return false, w.noteWaiting(ctx, work, owner, "sandbox_wake_outcome_unknown")
	default:
		return false, w.noteWaiting(ctx, work, owner, "sandbox_suspended")
	}
}

func optionalIncarnation(incarnation string) *string {
	if incarnation == "" {
		return nil
	}
	return &incarnation
}

func (w *DeliveryWorker) waitForRequestedWake(
	ctx context.Context,
	work queuedWork,
	owner Owner,
	sandbox SandboxSnapshot,
	blocker *environmentpb.DeliveryBlocker,
) (bool, error) {
	requestedAt := work.wakeRequestedAt
	if requestedAt == nil {
		observedAt := work.wakeAssessment.GetObservedAt()
		if observedAt == nil {
			return false, w.noteWaiting(ctx, work, owner, "sandbox_resuming")
		}
		storedAt, err := w.Store.NoteWakeRequestedAt(ctx, work.id, owner, work.session, observedAt.AsTime())
		if err != nil || storedAt == nil {
			return false, err
		}
		requestedAt = storedAt
	}
	failed := completeAssessmentObservation(sandbox, assessmentFor(sandbox, work.wakeAssessment,
		environmentpb.WakeState_WAKE_STATE_FAILED,
		environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE,
		status.New(codes.DeadlineExceeded, "Sandbox did not become Ready before the wake deadline").Proto()))
	_, _, remaining, err := w.Store.FailExpiredWake(ctx, work.id, owner, work.session,
		"wake_deadline_expired: "+sandbox.ReadyDetail, nil, failed)
	if err != nil {
		return false, err
	}
	if remaining <= 0 {
		w.clearRetry(work.key)
		return false, nil
	}
	w.queue.AddAfter(work.key, remaining)
	return false, w.noteWaiting(ctx, work, owner, "sandbox_resuming")
}

func assessmentFor(
	sandbox SandboxSnapshot,
	previous *environmentpb.WakeAssessment,
	state environmentpb.WakeState,
	reason environmentpb.WakeReason,
	errStatus *statuspb.Status,
) *environmentpb.WakeAssessment {
	assessment := &environmentpb.WakeAssessment{
		State: state, Reason: reason, ObservedAt: timestamppb.Now(),
		TargetGeneration: proto.Int64(sandbox.WakeGeneration), Error: errStatus,
	}
	if previous != nil {
		old := proto.Clone(previous).(*environmentpb.WakeAssessment)
		newValue := proto.Clone(assessment).(*environmentpb.WakeAssessment)
		old.ObservedAt, newValue.ObservedAt = nil, nil
		if proto.Equal(old, newValue) {
			return previous
		}
	}
	return assessment
}

func completeAssessmentObservation(sandbox SandboxSnapshot, assessment *environmentpb.WakeAssessment) *environmentpb.WakeAssessment {
	if assessment == nil {
		return nil
	}
	if assessment.GetObservedAt() != nil && assessment.TargetGeneration != nil {
		return assessment
	}
	result := proto.Clone(assessment).(*environmentpb.WakeAssessment)
	if result.GetObservedAt() == nil {
		result.ObservedAt = timestamppb.Now()
	}
	if result.TargetGeneration == nil {
		result.TargetGeneration = proto.Int64(sandbox.WakeGeneration)
	}
	return result
}

func preserveObservationTime(previous, current *environmentpb.WakeAssessment) *environmentpb.WakeAssessment {
	if previous == nil || current == nil {
		return current
	}
	old := proto.Clone(previous).(*environmentpb.WakeAssessment)
	newValue := proto.Clone(current).(*environmentpb.WakeAssessment)
	old.ObservedAt, newValue.ObservedAt = nil, nil
	if proto.Equal(old, newValue) {
		return previous
	}
	if current.GetObservedAt() == nil {
		current.ObservedAt = timestamppb.Now()
	}
	return current
}

func (w *DeliveryWorker) recordAssessment(
	ctx context.Context,
	work queuedWork,
	owner Owner,
	blocker *environmentpb.DeliveryBlocker,
	assessment *environmentpb.WakeAssessment,
) error {
	if assessment == nil {
		assessment = &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED}
	}
	assessment = preserveObservationTime(work.wakeAssessment, assessment)
	return w.Store.SetDeliveryAssessment(ctx, work.id, owner, work.session, blocker, assessment)
}

func (w *DeliveryWorker) scheduleWakeDeadline(key string, requestedAt time.Time) {
	remaining := time.Until(requestedAt.Add(WakeDeadline))
	if remaining < 0 {
		remaining = 0
	}
	w.queue.AddAfter(key, remaining)
}
