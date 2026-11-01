package stream

import (
	"context"
	"time"

	"github.com/sethvargo/go-retry"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Retry reconnects a long-lived stream. Local failures are returned to the
// owner; other failures are retried, resetting backoff after reported progress.
func Retry(ctx context.Context, name string, attempt func(context.Context) (bool, error), localFailure func(error) bool) error {
	newBackoff := func() retry.Backoff {
		return retry.WithFullJitter(retry.WithCappedDuration(30*time.Second, retry.NewExponential(500*time.Millisecond)))
	}
	backoff := newBackoff()
	var lastError error
	waits := retry.BackoffFunc(func() (time.Duration, bool) {
		wait, stop := backoff.Next()
		if code := status.Code(lastError); code == codes.PermissionDenied || code == codes.FailedPrecondition {
			wait = 30 * time.Second
		}
		zap.L().Warn("runner stream lost", zap.String("stream", name), zap.Error(lastError), zap.Duration("retry_after", wait))
		return wait, stop
	})
	err := retry.Do(ctx, waits, func(ctx context.Context) error {
		progress, err := attempt(ctx)
		if err == nil || (localFailure != nil && localFailure(err)) {
			return err
		}
		if progress {
			backoff = newBackoff()
		}
		lastError = err
		return retry.RetryableError(err)
	})
	if ctx.Err() != nil {
		return nil
	}
	return err
}
