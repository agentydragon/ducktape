// Package worker delivers notices for entries already committed to the Notification Service's inboxes.
//
// Every durable decision is in the store's rows, so any replica can resume any inbox: a worker claims an inbox
// under a 30-second lease, every write it makes is fenced by that lease, and what it does next is scheduled by
// its release.
package worker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sessionidentity"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
)

// stepBudget bounds the work for one claim. The lease outlasts it by ten seconds, which the fenced calls that
// record its outcome use: a worker past its lease can commit nothing.
const stepBudget = store.LeaseDuration - 10*time.Second

const (
	// storageRetry is the wait after the database fails.
	storageRetry    = 5 * time.Second
	cleanupInterval = time.Minute
	// releaseGrace bounds giving a claim back, which a stop does not interrupt.
	releaseGrace = 5 * time.Second
)

// Worker claims inboxes and works them.
type Worker struct {
	Store *store.Store
	// Namespace is the one namespace whose Sandboxes inboxes may name.
	Namespace string
	Sandboxes Sandboxes
	History   History
	// Dispatcher submits notices. Its calls are idempotent by command ID.
	Dispatcher Dispatcher
	Settings   Settings
}

// Run delivers notices until ctx ends, which returns nil. A delivery failure is retried by the schedule the
// failure leaves in the rows. A database failure is retried after five seconds.
//
// changes signals that work was committed on some replica (store.WorkChannel). Run reads the rows after
// clearing it, so a commit that races with a query leaves a signal for the next wait.
func (w *Worker) Run(ctx context.Context, changes <-chan struct{}) error {
	var cleanup cleanupState
	for {
		select {
		case <-changes:
		default:
		}
		if !time.Now().Before(cleanup.due) {
			after, err := w.Store.Cleanup(ctx, cleanup.after)
			if ctx.Err() != nil {
				return nil
			}
			if err != nil {
				slog.WarnContext(ctx, "notification inbox retention unavailable; retrying", "error", err)
				if !wait(ctx, changes, storageRetry) {
					return nil
				}
				continue
			}
			cleanup.after = after
			cleanup.due = time.Now().Add(cleanupInterval)
		}
		worked, err := w.ProcessOne(ctx)
		switch {
		case ctx.Err() != nil:
			return nil
		case err != nil:
			slog.WarnContext(ctx, "notification storage unavailable; retrying", "error", err)
		case worked:
			continue
		}

		idle := storageRetry
		if err == nil {
			idle = time.Until(cleanup.due)
			next, nextErr := w.Store.NextWorkAt(ctx)
			if ctx.Err() != nil {
				return nil
			}
			if nextErr != nil {
				slog.WarnContext(ctx, "notification storage unavailable; retrying", "error", nextErr)
				idle = storageRetry
			} else if next != nil {
				idle = min(idle, time.Until(*next))
			}
		}
		if !wait(ctx, changes, max(idle, 0)) {
			return nil
		}
	}
}

type cleanupState struct {
	after *uuid.UUID
	due   time.Time
}

func wait(ctx context.Context, changes <-chan struct{}, duration time.Duration) bool {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-changes:
		return true
	case <-timer.C:
		return true
	}
}

// attended is what working one claim leaves to do.
type attended struct {
	// deliveryError is what the inbox shows after this step.
	deliveryError *string
	// released is whether the claim was already given back, or lost.
	released bool
}

// ProcessOne delivers at most one due inbox notice. Run calls this same bounded operation.
func (w *Worker) ProcessOne(ctx context.Context) (bool, error) {
	claim, err := w.Store.Claim(ctx)
	if err != nil || claim == nil {
		return false, err
	}
	outcome, failure := w.attend(ctx, *claim)
	// A stop that lands while the claim is being given back must not fail the release: Run does not try again
	// once stopped, and the lease would hold the inbox for thirty seconds.
	grace, cancel := context.WithTimeout(context.WithoutCancel(ctx), releaseGrace)
	defer cancel()
	if shutdown := ctx.Err(); shutdown != nil {
		// What interrupted the work is not an outcome. The claim is given back all the same, so another replica
		// need not wait out the lease.
		released := w.release(grace, *claim, carriedError(*claim))
		if released != nil {
			// Run returns nil once stopped, so this log is the only trace that the inbox stays held until its
			// lease runs out. The error text can carry the connection string; the type names the failure.
			slog.WarnContext(ctx, "notification claim not released on stop", "inbox", claim.ID, "error_type", fmt.Sprintf("%T", released))
		}
		return true, errors.Join(shutdown, released)
	}
	if !outcome.released {
		failure = errors.Join(failure, w.release(grace, *claim, outcome.deliveryError))
	}
	return true, failure
}

// release gives the claim back with the next deadline computed from the rows. A claim that was lost has nothing
// to give back.
func (w *Worker) release(ctx context.Context, claim store.Claim, deliveryError *string) error {
	due, err := w.noticeDueAt(ctx, claim)
	if err == nil {
		err = w.Store.Release(ctx, claim, deliveryError, due)
	}
	if errors.Is(err, store.ErrClaimLost) {
		return nil
	}
	return err
}

// carriedError is the delivery error a claim keeps through a step whatever the step does: history that cannot
// be followed stays so.
func carriedError(claim store.Claim) *string {
	if claim.DeliveryError != nil && strings.HasPrefix(*claim.DeliveryError, store.InvalidHistoryPrefix) {
		return claim.DeliveryError
	}
	return nil
}

// attend works a claimed inbox within the step budget, and classifies how delivery ended.
func (w *Worker) attend(ctx context.Context, claim store.Claim) (attended, error) {
	// While the history cannot be followed, notices are prepared but never delivered.
	outcome := attended{deliveryError: carriedError(claim)}
	budget, cancel := context.WithTimeout(ctx, stepBudget)
	defer cancel()
	err := w.work(budget, claim, outcome.deliveryError == nil)

	var stale *staleDestinationError
	var invalid *store.InvalidHistoryError
	var unavailableFailure *unavailableError
	failed := func(name string) {
		if outcome.deliveryError == nil {
			outcome.deliveryError = &name
		}
		slog.WarnContext(ctx, "notification delivery unavailable", "inbox", claim.ID, "cause", *outcome.deliveryError)
	}
	switch {
	case err == nil:
	case errors.Is(err, store.ErrClaimLost):
		outcome.released = true
	case budget.Err() != nil && ctx.Err() == nil:
		failed(timeoutName)
	case errors.As(err, &stale):
		observeErr := w.Store.ObserveStale(ctx, claim, stale.reason, seconds(w.Settings.UnavailableDestinationRetrySeconds))
		outcome.released = true
		if !errors.Is(observeErr, store.ErrClaimLost) {
			return outcome, observeErr
		}
	case errors.Is(err, errOwnerMismatch):
		// The destination is the Sandbox it names, so its unavailable state is over.
		clearErr := w.Store.ClearStale(ctx, claim)
		name := "OwnerMismatchError"
		outcome.deliveryError = &name
		slog.WarnContext(ctx, "notification delivery unavailable", "inbox", claim.ID, "cause", name)
		if !errors.Is(clearErr, store.ErrClaimLost) {
			return outcome, clearErr
		}
	case errors.As(err, &invalid):
		message := invalid.Error()
		outcome.deliveryError = &message
	case errors.Is(err, errDestinationRejected):
		failed("DestinationRejectedError")
	case errors.As(err, &unavailableFailure):
		failed(unavailableFailure.name)
	default:
		return outcome, err
	}
	return outcome, nil
}

// work is the step's policy for one inbox. deliver is false while the inbox's history is known to be invalid.
func (w *Worker) work(ctx context.Context, claim store.Claim, deliver bool) error {
	if claim.DestinationRef.Namespace != w.Namespace {
		return errDestinationRejected
	}
	session, err := w.Dispatcher.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: claim.SessionID})
	if err != nil {
		return unavailable(err)
	}
	if err := w.checkSessionOwner(claim, session.GetSession()); err != nil {
		return err
	}
	destinationErr := w.checkDestination(ctx, claim)
	var stale *staleDestinationError
	if destinationErr != nil && !errors.As(destinationErr, &stale) {
		return destinationErr
	}
	if stale == nil {
		if err := w.Store.ClearStale(ctx, claim); err != nil {
			return err
		}
	}
	notice, err := w.prepareNotice(ctx, claim)
	if err != nil {
		return err
	}
	if stale != nil {
		return stale
	}
	if notice == nil || !deliver {
		return nil
	}
	return w.deliver(ctx, claim, claim.DestinationRef.Name, *notice)
}

// checkSessionOwner checks the claimed inbox's already-persisted Identity against what the Session authority recorded.
func (w *Worker) checkSessionOwner(claim store.Claim, session *dispatcherpb.Session) error {
	identity, ok := sessionidentity.IdentityRun(session, claim.SessionID, claim.DestinationRef)
	if !ok || claim.OwnerIdentity != identity {
		return errOwnerMismatch
	}
	return nil
}
