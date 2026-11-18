package protocol

import (
	"errors"
	"fmt"
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

// ProviderPhaseTimeoutError identifies an expired provider phase budget.
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
