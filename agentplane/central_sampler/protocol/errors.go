package protocol

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Errors are distinguished by callers at the service boundary.
var (
	ErrMissing      = errors.New("resource not found")
	ErrPrecondition = errors.New("resource is not in a state that permits this operation")
	ErrInvalid      = errors.New("request is invalid")
)

type UnknownEffectError struct{ Reason string }

func (e *UnknownEffectError) Error() string {
	if e.Reason == "" {
		return "remote effect outcome is unknown; automatic replay is unsafe"
	}
	return e.Reason
}

// KnownFailureError records a provider or pre-dispatch rejection whose effect is known.
type KnownFailureError struct {
	Evidence map[string]any
	Message  string
}

// ProviderTransientError marks an upstream transport, status, or truncated-stream failure that
// is safe to resample because provider sampling has no external mutation effect.
type ProviderTransientError struct {
	Cause      error
	RetryAfter time.Duration
}

func (e *ProviderTransientError) Error() string {
	if e.Cause == nil {
		return "transient provider failure"
	}
	return fmt.Sprintf("transient provider failure: %v", e.Cause)
}

func (e *ProviderTransientError) Unwrap() error { return e.Cause }

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

func (e *KnownFailureError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return fmt.Sprint(e.Evidence)
}

type ProviderTimeoutPhase string

const (
	ProviderTimeoutResponseHeader ProviderTimeoutPhase = "response_header"
	ProviderTimeoutSemanticIdle   ProviderTimeoutPhase = "semantic_idle"
)

// ProviderPhaseTimeoutError distinguishes a provider phase budget from the turn deadline.
type ProviderPhaseTimeoutError struct {
	Phase   ProviderTimeoutPhase
	Timeout time.Duration
	Cause   error
}

func (e *ProviderPhaseTimeoutError) Error() string {
	return fmt.Sprintf("provider %s timeout after %s: %v", e.Phase, e.Timeout, e.Cause)
}

func (e *ProviderPhaseTimeoutError) Unwrap() error { return e.Cause }

// ProviderProgressError marks a failure renewing durable provider progress. It must not be
// classified as an upstream transport failure.
type ProviderProgressError struct{ Cause error }

func (e *ProviderProgressError) Error() string {
	return fmt.Sprintf("record provider progress: %v", e.Cause)
}

func (e *ProviderProgressError) Unwrap() error { return e.Cause }

// ProviderEvidencePersistenceError identifies failure to durably record provider evidence.
type ProviderEvidencePersistenceError struct{ Cause error }

func (e *ProviderEvidencePersistenceError) Error() string {
	return fmt.Sprintf("persist provider evidence: %v", e.Cause)
}

func (e *ProviderEvidencePersistenceError) Unwrap() error { return e.Cause }

// ProviderContextError reports a typed phase cancellation when a stream operation observes its
// canceled context. It preserves other errors, including evidence persistence failures.
func ProviderContextError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	var evidenceErr *ProviderEvidencePersistenceError
	var progressErr *ProviderProgressError
	if errors.As(err, &evidenceErr) || errors.As(err, &progressErr) {
		return err
	}
	var timeout *ProviderPhaseTimeoutError
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
	var evidenceErr *ProviderEvidencePersistenceError
	var progressErr *ProviderProgressError
	var phaseErr *ProviderPhaseTimeoutError
	if errors.As(err, &evidenceErr) || errors.As(err, &progressErr) || errors.As(err, &phaseErr) || ctx.Err() != nil {
		return err
	}
	return &ProviderTransientError{Cause: err}
}
