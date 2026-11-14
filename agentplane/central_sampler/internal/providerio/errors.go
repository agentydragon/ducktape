package providerio

import (
	"context"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

// ParseProviderRetryAfter parses the delta-seconds and HTTP-date forms. The caller applies the
// provider policy's finite upper bound before persisting it.
func ParseProviderRetryAfter(value string) time.Duration {
	value = strings.TrimSpace(value)
	isDeltaSeconds := value != ""
	for _, char := range value {
		if char < '0' || char > '9' {
			isDeltaSeconds = false
			break
		}
	}
	if isDeltaSeconds {
		seconds, err := strconv.ParseUint(value, 10, 64)
		if err != nil || seconds > uint64((1<<63-1)/int64(time.Second)) {
			return time.Duration(1<<63 - 1)
		}
		if seconds == 0 {
			return 0
		}
		return time.Duration(seconds) * time.Second
	}
	when, err := http.ParseTime(value)
	if err != nil {
		return 0
	}
	if delay := time.Until(when); delay > 0 {
		return delay
	}
	return 0
}

// ProviderContextError reports a typed phase cancellation when a stream operation observes its
// canceled context. It preserves other errors, including evidence persistence failures.
func ProviderContextError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	var evidenceErr *protocol.ProviderEvidencePersistenceError
	var progressErr *protocol.ProviderProgressError
	if errors.As(err, &evidenceErr) || errors.As(err, &progressErr) {
		return err
	}
	var timeout *protocol.ProviderPhaseTimeoutError
	if cause := context.Cause(ctx); errors.As(cause, &timeout) && (errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded)) {
		return cause
	}
	return err
}

// ProviderTransportError wraps only upstream read/transport errors. Durable evidence and progress
// failures, typed phase timeouts, and caller cancellation retain their distinct meanings.
func ProviderTransportError(ctx context.Context, err error) error {
	err = ProviderContextError(ctx, err)
	if err == nil {
		return nil
	}
	var evidenceErr *protocol.ProviderEvidencePersistenceError
	var progressErr *protocol.ProviderProgressError
	var phaseErr *protocol.ProviderPhaseTimeoutError
	if errors.As(err, &evidenceErr) || errors.As(err, &progressErr) || errors.As(err, &phaseErr) || ctx.Err() != nil {
		return err
	}
	return &protocol.ProviderTransientError{Cause: err}
}
