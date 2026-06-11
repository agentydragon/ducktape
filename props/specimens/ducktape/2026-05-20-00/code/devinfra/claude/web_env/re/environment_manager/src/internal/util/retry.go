package util

import (
	"log/slog"
	"time"
)

// Reconstructed from symbol: internal/util.RetryOperation
//
// RetryOperation retries the given operation up to maxRetries times with
// exponential backoff starting at baseDelay. If shouldRetry is non-nil,
// it is called with the error to determine if the operation should be retried;
// if it returns false, the error is returned immediately without further retries.
// Extra slog attributes are appended to all log messages.
func RetryOperation(ctx interface{ Value(any) any }, msg string, operation func() error, maxRetries int, baseDelay time.Duration, shouldRetry func(error) bool, extraAttrs ...slog.Attr) (error, slog.Attr) {
	var lastErr error
	var lastErrAttr slog.Attr

	for attempt := 0; attempt < maxRetries; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(1<<uint(attempt-1)) * baseDelay
			backoffMs := backoff.Milliseconds()

			attrs := make([]any, 0, len(extraAttrs)+4)
			attrs = append(attrs,
				slog.Int("attempt", attempt+1),
				slog.Int64("delay_ms", backoffMs),
			)
			for _, a := range extraAttrs {
				attrs = append(attrs, a)
			}
			slog.Warn("operation_attempt_failed", attrs...)

			time.Sleep(backoff)
		}

		err := operation()
		if err == nil {
			return nil, slog.Attr{}
		}

		lastErrAttr = slog.Any("error", err)

		if shouldRetry != nil && !shouldRetry(err) {
			attrs := make([]any, 0, len(extraAttrs)+6)
			attrs = append(attrs,
				slog.Int("attempt", attempt+1),
				slog.Any("error", err),
				slog.String("reason", "non-retryable"),
			)
			for _, a := range extraAttrs {
				attrs = append(attrs, a)
			}
			slog.Debug("skipping_retry_for_non_retryable_error", attrs...)
			return err, lastErrAttr
		}

		attrs := make([]any, 0, len(extraAttrs)+4)
		attrs = append(attrs,
			slog.Int("attempt", attempt+1),
			slog.Any("error", err),
		)
		for _, a := range extraAttrs {
			attrs = append(attrs, a)
		}
		slog.Debug("retrying_operation", attrs...)

		lastErr = err
	}

	attrs := make([]any, 0, len(extraAttrs)+4)
	attrs = append(attrs,
		slog.Int("attempts", maxRetries),
		slog.Any("error", lastErr),
	)
	for _, a := range extraAttrs {
		attrs = append(attrs, a)
	}
	slog.Error("operation_failed_after_retries", attrs...)

	return lastErr, lastErrAttr
}
