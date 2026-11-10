package protocol

import (
	"errors"
	"fmt"
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

func (e *KnownFailureError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return fmt.Sprint(e.Evidence)
}
