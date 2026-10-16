package dispatcher

import (
	"context"
	"errors"
	"time"
)

type queuedWork struct {
	id, key         string
	opening         bool
	wakeEligible    bool
	wakeCaller      *Account
	wakeFrom        *string
	wakeRequestedAt *time.Time
	operation       string
}

func (w *DeliveryWorker) prepareDispatch(ctx context.Context, row *Dispatch, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	return w.prepare(ctx, queuedWork{
		row.CommandID, row.SessionID, false, row.WakeEligible, row.WakeCaller,
		row.WakeFromIncarnation, row.WakeRequestedAt, row.Operation,
	}, sandbox, owner)
}

func (w *DeliveryWorker) prepareOpening(ctx context.Context, row *Opening, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	return w.prepare(ctx, queuedWork{
		row.SessionID, row.SessionID, true, row.WakeEligible, row.WakeCaller,
		row.WakeFromIncarnation, row.WakeRequestedAt, "open_session",
	}, sandbox, owner)
}

func (work queuedWork) workID() string {
	if work.opening {
		return "opening:" + work.id
	}
	return work.id
}

func awaitingNewIncarnation(sandbox SandboxSnapshot, from *string) bool {
	if from == nil {
		return false
	}
	return sandbox.Incarnation == "" || (*from != "" && sandbox.Incarnation == *from)
}

func (w *DeliveryWorker) noteWaiting(ctx context.Context, work queuedWork, owner Owner, reason string) error {
	if work.opening {
		return w.Store.NoteOpeningWaiting(ctx, work.id, owner, reason)
	}
	return w.Store.NoteWaiting(ctx, work.id, owner, reason)
}

func (w *DeliveryWorker) prepare(ctx context.Context, work queuedWork, sandbox SandboxSnapshot, owner Owner) (bool, error) {
	if sandbox.Deleting {
		w.clearDelay(work.key)
		return false, w.noteWaiting(ctx, work, owner, "sandbox_deleted")
	}
	awaiting := awaitingNewIncarnation(sandbox, work.wakeFrom)
	wakeDisabled := work.wakeEligible && !sandbox.WakeOnSend && (awaiting || sandbox.Blocker == "sandbox_suspended")
	if wakeDisabled {
		w.clearDelay(work.key)
		if err := w.Store.ClearWakeState(ctx, work.id, owner, work.opening); err != nil {
			return false, err
		}
		awaiting = false
	} else if work.wakeRequestedAt != nil {
		if !awaiting && sandbox.Blocker == "" {
			w.clearDelay(work.key)
			if err := w.Store.ClearWakeState(ctx, work.id, owner, work.opening); err != nil {
				return false, err
			}
		} else {
			reason := "wake_deadline_expired: " + sandbox.ReadyDetail
			failedDispatch, failedOpening, remaining, err := w.Store.FailExpiredWake(ctx, work.id, owner, work.opening, reason)
			if err != nil {
				return false, err
			}
			if failedDispatch != nil || failedOpening != nil || remaining <= 0 {
				w.clearRetry(work.key)
				return false, nil
			}
			w.queue.AddAfter(work.key, remaining)
		}
	}
	if delay := w.retryRemaining(work.key); delay > 0 && !wakeDisabled {
		w.queue.AddAfter(work.key, delay)
		return false, nil
	}
	if awaiting {
		if work.wakeEligible {
			return false, w.wakeFor(ctx, work, sandbox, owner)
		}
		return false, w.noteWaiting(ctx, work, owner, "sandbox_resuming")
	}
	if sandbox.Blocker != "" {
		if sandbox.Blocker == "sandbox_suspended" && work.operation == "stop_runner_session" {
			w.clearRetry(work.key)
			_, err := w.Store.Settle(ctx, work.id, owner, []string{QueueState}, "NO_OP", "sandbox_suspended", nil)
			return false, err
		}
		if sandbox.Blocker == "sandbox_suspended" && work.wakeEligible {
			return false, w.wakeFor(ctx, work, sandbox, owner)
		}
		return false, w.noteWaiting(ctx, work, owner, sandbox.Blocker)
	}
	if w.refusedHere(sandbox.UID, sandbox.Incarnation) {
		return false, w.noteWaiting(ctx, work, owner, "runner_refused_work")
	}
	return true, nil
}

func (w *DeliveryWorker) wakeFor(ctx context.Context, work queuedWork, sandbox SandboxSnapshot, owner Owner) error {
	if work.wakeCaller == nil {
		return w.noteWaiting(ctx, work, owner, "wake_caller_unknown")
	}
	var incarnation *string
	if sandbox.Incarnation != "" {
		incarnation = &sandbox.Incarnation
	}
	stillQueued, err := w.Store.NoteWaking(ctx, work.id, owner, incarnation, work.opening)
	if err != nil {
		return err
	}
	if !stillQueued {
		w.clearRetry(work.key)
		return nil
	}
	stillOwns, err := w.Leases.StillOwns(ctx, owner)
	if err != nil {
		return err
	}
	if !stillOwns {
		return nil
	}
	requestedAt := work.wakeRequestedAt
	beforeResume := func(generation int64) error {
		current, err := w.Leases.StillOwns(ctx, owner)
		if err != nil {
			return err
		}
		if !current {
			return ErrWakeSuperseded
		}
		retryAfter, err := w.Store.ReserveWakeAttempt(ctx, *work.wakeCaller, sandbox.UID, generation)
		if err != nil {
			return err
		}
		if retryAfter > 0 {
			return wakeLimitedError{retryAfter}
		}
		requestedAt, err = w.Store.NoteWakeRequested(ctx, work.id, owner, work.opening)
		if err != nil {
			return err
		}
		if requestedAt == nil {
			return ErrWakeSuperseded
		}
		w.scheduleWakeDeadline(work.key, *requestedAt)
		return nil
	}
	requested, err := w.Wake.WakeForSend(ctx, sandbox.Name, sandbox.UID, beforeResume)
	if err != nil {
		var limited wakeLimitedError
		switch {
		case errors.Is(err, ErrWakeSuperseded):
			w.clearRetry(work.key)
			return nil
		case errors.Is(err, ErrWakeQuiescing):
			return w.noteWaiting(ctx, work, owner, "sandbox_resuming")
		case errors.Is(err, ErrWakeModeUnknown):
			return w.noteWaiting(ctx, work, owner, "sandbox_not_ready")
		case errors.Is(err, ErrWakeDeleted):
			w.clearDelay(work.key)
			return w.noteWaiting(ctx, work, owner, "sandbox_deleted")
		case errors.As(err, &limited):
			w.setRetry(work.key, work.workID(), limited.retryAfter)
			return w.noteWaiting(ctx, work, owner, "wake_rate_limited")
		default:
			w.backoff(work.key, work.workID())
			w.Logger.Warn("Sandbox wake request unavailable", "sandbox", sandbox.Name, "error", err)
			return w.noteWaiting(ctx, work, owner, "delivery_unavailable")
		}
	}
	if !requested {
		w.clearDelay(work.key)
		return w.noteWaiting(ctx, work, owner, "wake_on_send_disabled")
	}
	if requestedAt == nil {
		requestedAt, err = w.Store.NoteWakeRequested(ctx, work.id, owner, work.opening)
		if err != nil {
			return err
		}
		if requestedAt == nil {
			return nil
		}
		w.scheduleWakeDeadline(work.key, *requestedAt)
	}
	w.clearDelay(work.key)
	return w.noteWaiting(ctx, work, owner, "sandbox_resuming")
}

func (w *DeliveryWorker) clearDelay(key string) {
	w.mu.Lock()
	delete(w.retryAfter, key)
	w.mu.Unlock()
}

func (w *DeliveryWorker) scheduleWakeDeadline(key string, requestedAt time.Time) {
	remaining := time.Until(requestedAt.Add(WakeDeadline))
	if remaining < 0 {
		remaining = 0
	}
	w.queue.AddAfter(key, remaining)
}
