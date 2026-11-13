// Package sources coordinates durable reads from independent subscription providers.
package sources

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	model "github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// stepBudget leaves ten seconds of the inbox lease for fenced writes and release.
const stepBudget = store.LeaseDuration - 10*time.Second

const (
	storageRetry = 5 * time.Second
	releaseGrace = 5 * time.Second
)

// Worker claims due source subscriptions and runs the provider named by the stored source_provider value.
type Worker struct {
	Store     *store.Store
	Providers map[string]Provider
}

// Run processes due source subscriptions until ctx ends. Store failures are retried; a provider failure outside
// its contract returns an UnclassifiedProviderError and stops the worker.
func (w *Worker) Run(ctx context.Context, changes <-chan struct{}) error {
	for {
		select {
		case <-changes:
		default:
		}
		worked, err := w.ProcessOne(ctx)
		var unclassified *UnclassifiedProviderError
		switch {
		case ctx.Err() != nil:
			return nil
		case errors.As(err, &unclassified):
			return err
		case err != nil:
			slog.WarnContext(ctx, "notification source storage unavailable; retrying", "error", err)
		case worked:
			continue
		}

		idle := storageRetry
		if err == nil {
			idle, err = w.idle(ctx)
			if ctx.Err() != nil {
				return nil
			}
			if err != nil {
				slog.WarnContext(ctx, "notification source storage unavailable; retrying", "error", err)
				idle = storageRetry
			}
		}
		var timer *time.Timer
		var deadline <-chan time.Time
		if idle >= 0 {
			timer = time.NewTimer(idle)
			deadline = timer.C
		}
		select {
		case <-ctx.Done():
		case <-changes:
		case <-deadline:
		}
		if timer != nil {
			timer.Stop()
		}
	}
}

func (w *Worker) idle(ctx context.Context) (time.Duration, error) {
	next, err := w.Store.NextSourceWorkAt(ctx)
	if err != nil {
		return 0, err
	}
	if next == nil {
		return -1, nil
	}
	return max(time.Until(*next), 0), nil
}

// ProcessOne claims and processes at most one due source subscription. Run calls this same bounded operation.
func (w *Worker) ProcessOne(ctx context.Context) (worked bool, err error) {
	claim, err := w.Store.ClaimSource(ctx)
	if err != nil || claim == nil {
		return false, err
	}
	defer func() {
		releaseContext, cancel := context.WithTimeout(context.WithoutCancel(ctx), releaseGrace)
		defer cancel()
		releaseErr := w.Store.ReleaseSource(releaseContext, *claim)
		if errors.Is(releaseErr, store.ErrClaimLost) {
			releaseErr = nil
		}
		if ctx.Err() != nil && releaseErr != nil {
			slog.WarnContext(ctx, "notification source claim not released on stop", "inbox", claim.ID,
				"error_type", fmt.Sprintf("%T", releaseErr))
		}
		err = errors.Join(err, releaseErr)
	}()

	due, err := w.Store.Source(ctx, *claim)
	if err != nil || due == nil {
		return true, err
	}
	provider, ok := w.Providers[due.SourceProvider]
	if !ok {
		return true, w.recordFailure(ctx, *claim, *due, &ProviderError{
			Kind: model.FailureUnavailable, Message: "source provider is disabled",
		})
	}

	budget, cancel := context.WithTimeout(ctx, stepBudget)
	processErr := provider.Process(budget, *claim, *due)
	budgetErr := budget.Err()
	cancel()
	if ctx.Err() != nil {
		return true, ctx.Err()
	}
	if budgetErr != nil && processErr != nil {
		return true, w.recordFailure(ctx, *claim, *due, &ProviderError{
			Kind: model.FailureUnavailable, Message: "source processing timed out",
		})
	}
	if processErr == nil || errors.Is(processErr, store.ErrClaimLost) {
		return true, nil
	}

	var deferred *ProviderDeferred
	var failure *ProviderError
	var quota *store.QuotaError
	switch {
	case errors.As(processErr, &deferred):
		return true, w.Store.SourceDeferred(ctx, *claim, due.Subscription, deferred.Until)
	case errors.As(processErr, &failure):
		return true, w.recordFailure(ctx, *claim, *due, failure)
	case errors.As(processErr, &quota):
		return true, w.recordFailure(ctx, *claim, *due, &ProviderError{
			Kind: model.FailureProcessingError, Message: "QuotaError",
		})
	default:
		return true, &UnclassifiedProviderError{SubscriptionID: due.ID, Err: processErr}
	}
}

func (w *Worker) recordFailure(ctx context.Context, claim store.Claim, due store.DueSubscription, failure *ProviderError) error {
	retry := failure.RetryAfter
	if retry == 0 {
		retry = DefaultRetry
	}
	if err := w.Store.SourceFailed(ctx, claim, due.Subscription, failure.Kind, failure.Message, retry); err != nil {
		return err
	}
	slog.WarnContext(ctx, "notification source retry", "inbox", claim.ID, "subscription", due.ID,
		"cause", failure.Message, "retry_seconds", retry.Seconds())
	return nil
}
