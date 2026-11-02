package dispatcher

import (
	"context"
	"errors"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type queuedWork struct {
	id, key         string
	opening         bool
	effectiveCaller *lifecycle.ServiceAccount
	wakeFrom        *string
	wakeRequestedAt *time.Time
	wakeAssessment  *environmentpb.WakeAssessment
}

func effectiveWakeCaller(attribution *AdmissionAttribution) *lifecycle.ServiceAccount {
	if attribution == nil {
		return nil
	}
	identity := attribution.EffectiveCaller
	if identity.Namespace == "" || identity.Name == "" || identity.UID == "" {
		return nil
	}
	return &lifecycle.ServiceAccount{Namespace: identity.Namespace, Name: identity.Name, UID: identity.UID}
}

func (w *DeliveryWorker) prepareDispatch(ctx context.Context, row *Dispatch, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	opening, err := w.Store.GetOpening(ctx, row.SessionID)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return false, err
	}
	work := queuedWork{
		id: row.CommandID, key: row.SessionID, effectiveCaller: effectiveWakeCaller(row.Attribution),
		wakeFrom: row.WakeFromIncarnation, wakeRequestedAt: row.WakeRequestedAt, wakeAssessment: row.WakeAssessment,
	}
	if opening != nil {
		if !sessionRequiresReadyExecutor(opening.Spec) {
			return w.prepareCentral(ctx, work, sandbox, owner)
		}
	}
	return w.prepare(ctx, work, sandbox, owner)
}

func (w *DeliveryWorker) prepareOpening(ctx context.Context, row *Opening, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	work := queuedWork{
		id: row.SessionID, key: row.SessionID, opening: true,
		effectiveCaller: effectiveWakeCaller(row.Attribution),
		wakeFrom:        row.WakeFromIncarnation, wakeRequestedAt: row.WakeRequestedAt, wakeAssessment: row.WakeAssessment,
	}
	if !sessionRequiresReadyExecutor(row.Spec) {
		return w.prepareCentral(ctx, work, sandbox, owner)
	}
	return w.prepare(ctx, work, sandbox, owner)
}

func (w *DeliveryWorker) prepareCentral(ctx context.Context, work queuedWork, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	assessment := assessmentFor(sandbox, work.wakeAssessment, environmentpb.WakeState_WAKE_STATE_NOT_NEEDED, 0, nil)
	if err := w.recordAssessment(ctx, work, owner, nil, assessment); err != nil {
		return false, err
	}
	if err := w.Store.ClearWakeState(ctx, work.id, owner, work.opening); err != nil {
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
	if work.opening {
		return "opening:" + work.id
	}
	return work.id
}

func (w *DeliveryWorker) noteWaiting(ctx context.Context, work queuedWork, owner Owner, reason string) error {
	if work.opening {
		return w.Store.NoteOpeningWaiting(ctx, work.id, owner, reason)
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
		if err := w.Store.ClearWakeState(ctx, work.id, owner, work.opening); err != nil {
			return false, err
		}
		return false, w.noteWaiting(ctx, work, owner, "sandbox_operator_requested_stop")
	}
	if sandbox.Blocker == "" {
		assessment := assessmentFor(sandbox, work.wakeAssessment, environmentpb.WakeState_WAKE_STATE_NOT_NEEDED, 0, nil)
		if err := w.recordAssessment(ctx, work, owner, nil, assessment); err != nil {
			return false, err
		}
		if err := w.Store.ClearWakeState(ctx, work.id, owner, work.opening); err != nil {
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
		(work.wakeAssessment.TargetGeneration == nil || work.wakeAssessment.GetTargetGeneration() == uint64(sandbox.WakeGeneration)) {
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

	if work.effectiveCaller == nil {
		assessment := assessmentFor(sandbox, work.wakeAssessment,
			environmentpb.WakeState_WAKE_STATE_DENIED,
			environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, nil)
		if err := w.recordAssessment(ctx, work, owner, blocker, assessment); err != nil {
			return false, err
		}
		return false, w.noteWaiting(ctx, work, owner, "sandbox_suspended")
	}
	if w.Wake == nil {
		return false, errors.New("lifecycle wake service is unavailable")
	}
	stillQueued, err := w.Store.NoteWaking(ctx, work.id, owner, optionalIncarnation(sandbox.Incarnation), work.opening)
	if err != nil || !stillQueued {
		return false, err
	}
	stillOwns, err := w.Leases.StillOwns(ctx, owner)
	if err != nil || !stillOwns {
		return false, err
	}
	assessment, err := w.Wake.RequestWake(ctx, lifecycle.Target{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: sandbox.UID,
	}, *work.effectiveCaller)
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
		storedAt, err := w.Store.NoteWakeRequestedAt(ctx, work.id, owner, work.opening, requestedAt)
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
		storedAt, err := w.Store.NoteWakeRequestedAt(ctx, work.id, owner, work.opening, observedAt.AsTime())
		if err != nil || storedAt == nil {
			return false, err
		}
		requestedAt = storedAt
	}
	failed := completeAssessmentObservation(sandbox, assessmentFor(sandbox, work.wakeAssessment,
		environmentpb.WakeState_WAKE_STATE_FAILED,
		environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE,
		status.New(codes.DeadlineExceeded, "Sandbox did not become Ready before the wake deadline").Proto()))
	_, _, remaining, err := w.Store.FailExpiredWake(ctx, work.id, owner, work.opening,
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
		TargetGeneration: proto.Uint64(uint64(sandbox.WakeGeneration)), Error: errStatus,
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
		result.TargetGeneration = proto.Uint64(uint64(sandbox.WakeGeneration))
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
	return w.Store.SetDeliveryAssessment(ctx, work.id, owner, work.opening, blocker, assessment)
}

func (w *DeliveryWorker) scheduleWakeDeadline(key string, requestedAt time.Time) {
	remaining := time.Until(requestedAt.Add(WakeDeadline))
	if remaining < 0 {
		remaining = 0
	}
	w.queue.AddAfter(key, remaining)
}
