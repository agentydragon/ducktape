// Package worker works the Notification Service's inboxes: it reads each subscription's source into its inbox,
// and tells the agent when entries await it with a notice that it submits to the Session's runner and follows
// to confirmation.
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

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/uuid"
)

// stepBudget bounds the work for one claim. The lease outlasts it by ten seconds, which the fenced calls that
// record its outcome use: a worker past its lease can commit nothing.
const stepBudget = store.LeaseDuration - 10*time.Second

const (
	// cleanupInterval is how often Run expires raw GitHub receipts, old inbox payloads and long-retired inboxes.
	cleanupInterval = time.Minute
	// storageRetry is the wait after the database fails.
	storageRetry = 5 * time.Second
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
	// Actions reads Action request subscriptions.
	Actions Provider
	// GitHub reads GitHub subscriptions, or is nil while the provider is off, which shows on each GitHub
	// subscription as an unavailable source.
	GitHub   Provider
	Settings Settings
}

// Run works inboxes until ctx ends, which returns nil. A claim, a source or a notice that fails is retried by
// the schedule the failure leaves in the rows. A database failure is retried after five seconds; a Provider
// failure outside its contract returns its *UnclassifiedProviderError.
//
// changes signals that work was committed on some replica (store.WorkChannel). Run reads the rows after
// clearing it, so a commit that races with a query leaves a signal for the next wait.
func (w *Worker) Run(ctx context.Context, changes <-chan struct{}) error {
	var sweep retention
	for {
		select {
		case <-changes:
		default:
		}
		idle, worked, err := w.advance(ctx, &sweep)
		var unclassified *UnclassifiedProviderError
		switch {
		case ctx.Err() != nil:
			return nil
		case errors.As(err, &unclassified):
			return err
		case err != nil:
			slog.WarnContext(ctx, "notification storage unavailable; retrying", "error", err)
			idle = storageRetry
		case worked:
			continue
		}
		timer := time.NewTimer(idle)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-changes:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// retention schedules cleanup of GitHub receipts and mergeability history, then pages through inbox retention,
// one batch a minute.
type retention struct {
	after *uuid.UUID
	due   time.Time
}

// advance runs the retention page if it is due, then one step. When there was nothing to work it returns how
// long to idle: until the next retention page or the next durable deadline, whichever is first.
func (w *Worker) advance(ctx context.Context, sweep *retention) (idle time.Duration, worked bool, err error) {
	if !time.Now().Before(sweep.due) {
		if days := w.Settings.GitHubDeliveryRetentionDays; days != nil {
			if err = w.Store.CleanupGitHubDeliveries(ctx, time.Now().Add(-time.Duration(*days)*24*time.Hour)); err != nil {
				return 0, false, err
			}
		}
		if sweep.after, err = w.Store.Cleanup(ctx, sweep.after); err != nil {
			return 0, false, err
		}
		sweep.due = time.Now().Add(cleanupInterval)
	}
	if worked, err = w.step(ctx); err != nil || worked {
		return 0, worked, err
	}
	idle = time.Until(sweep.due)
	next, err := w.Store.NextWorkAt(ctx)
	if err != nil {
		return 0, false, err
	}
	if next != nil {
		idle = min(idle, time.Until(*next))
	}
	return max(idle, 0), false, nil
}

// attended is what working one claim leaves to do.
type attended struct {
	// deliveryError is what the inbox shows after this step.
	deliveryError *string
	// released is whether the claim was already given back, or lost.
	released bool
}

// step works the inbox that has waited longest, if one is due, and reports whether there was one. The order is
// the delivery policy: the destination is checked, the next due source is read, then the notice is prepared
// once its debounce has run out and delivered; and whatever happens, the claim is released with the next
// deadline computed from the rows.
func (w *Worker) step(ctx context.Context) (bool, error) {
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

// attend works a claimed inbox within the step budget, and classifies how it ended. The error it returns is
// the one the step must not hide: the database failing, or a Provider breaking its contract.
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
		retired, observeErr := w.Store.ObserveStale(ctx, claim, stale.reason, seconds(w.Settings.StaleInboxConfirmationSeconds))
		outcome.released = true
		if retired {
			slog.InfoContext(ctx, "retired stale notification inbox", "inbox", claim.ID, "reason", stale.reason)
		}
		if !errors.Is(observeErr, store.ErrClaimLost) {
			return outcome, observeErr
		}
	case errors.Is(err, errOwnerMismatch):
		// The destination is the Sandbox it names, so a pending retirement is over.
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
	if err := w.checkDestination(ctx, claim); err != nil {
		return err
	}
	if err := w.Store.ClearStale(ctx, claim); err != nil {
		return err
	}
	if err := w.readSource(ctx, claim); err != nil {
		return err
	}
	notice, err := w.prepareNotice(ctx, claim)
	if err != nil || notice == nil || !deliver {
		return err
	}
	return w.deliver(ctx, claim, claim.DestinationRef.Name, *notice)
}

// readSource reads the claimed inbox's next due subscription, if any, and records the outcome on it.
func (w *Worker) readSource(ctx context.Context, claim store.Claim) error {
	due, err := w.Store.Source(ctx, claim)
	if err != nil || due == nil {
		return err
	}
	err = w.reconcile(ctx, claim, *due)

	var deferred *ProviderDeferred
	var failure *ProviderError
	var quota *store.QuotaError
	switch {
	case err == nil:
		return nil
	case errors.As(err, &deferred):
		return w.Store.SourceDeferred(ctx, claim, due.Subscription, deferred.Until)
	case errors.As(err, &failure):
		return w.sourceFailed(ctx, claim, *due, failure.Kind, failure.Message, failure.RetryAfter)
	case errors.As(err, &quota):
		return w.sourceFailed(ctx, claim, *due, store.FailureProcessingError, "QuotaError", 0)
	case errors.Is(err, store.ErrClaimLost) || ctx.Err() != nil:
		return err
	default:
		return &UnclassifiedProviderError{SubscriptionID: due.ID, Err: err}
	}
}

func (w *Worker) reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription) error {
	switch due.Source.(type) {
	case model.ActionsSource:
		return w.Actions.Reconcile(ctx, claim, due)
	case model.GitHubSource:
		if w.GitHub == nil {
			return &ProviderError{Kind: store.FailureUnavailable, Message: "GitHub provider is disabled"}
		}
		return w.GitHub.Reconcile(ctx, claim, due)
	default:
		return errors.New("subscription has no source")
	}
}

func (w *Worker) sourceFailed(ctx context.Context, claim store.Claim, due store.DueSubscription, kind store.SourceFailureKind, message string, retryAfter time.Duration) error {
	if retryAfter == 0 {
		retryAfter = DefaultRetry
	}
	if err := w.Store.SourceFailed(ctx, claim, due.Subscription, kind, message, retryAfter); err != nil {
		return err
	}
	slog.WarnContext(ctx, "notification source retry", "inbox", claim.ID, "subscription", due.ID, "cause", message,
		"retry_seconds", retryAfter.Seconds())
	return nil
}
