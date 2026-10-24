package bridge

import (
	"fmt"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// MalformedMessageError means request fields are not valid proto-JSON for the
// expected message.
type MalformedMessageError struct{ Cause error }

func (e *MalformedMessageError) Error() string {
	return fmt.Sprintf("not a SessionSpec: %v", e.Cause)
}

func (e *MalformedMessageError) Unwrap() error { return e.Cause }

// SessionOpeningNotFoundError hides an opening that belongs to another
// Sandbox, or whose returned identity does not match the requested Session.
type SessionOpeningNotFoundError struct {
	SessionID uuid.UUID
	Sandbox   string
}

func (e *SessionOpeningNotFoundError) Error() string {
	return fmt.Sprintf("session opening %s was not found in Sandbox %q", e.SessionID, e.Sandbox)
}

// ThreadNotFoundError means the app has no Thread row for this History Session.
type ThreadNotFoundError struct{ ThreadID uuid.UUID }

func (e *ThreadNotFoundError) Error() string {
	return fmt.Sprintf("thread %s was not found", e.ThreadID)
}

// ThreadStateError means the History feed rejected the runner state.
type ThreadStateError struct{ Message string }

func (e *ThreadStateError) Error() string { return e.Message }

// RequestTimeoutError means an Agentplane unary call did not finish before its
// deadline. A mutation may still have committed.
type RequestTimeoutError struct{ Cause error }

func (e *RequestTimeoutError) Error() string {
	if e.Cause == nil {
		return "request deadline exceeded; outcome uncertain"
	}
	return "request deadline exceeded; outcome uncertain: " + e.Cause.Error()
}

func (e *RequestTimeoutError) Unwrap() error { return e.Cause }

// DispatcherRequestTimeoutError carries the idempotency key needed to
// reconcile or retry a timed-out Dispatcher mutation.
type DispatcherRequestTimeoutError struct {
	SessionID uuid.UUID
	CommandID string
	Cause     error
}

func (e *DispatcherRequestTimeoutError) Error() string {
	if e.CommandID != "" {
		return fmt.Sprintf("Dispatcher result for command '%s' was not confirmed; outcome uncertain", e.CommandID)
	}
	return fmt.Sprintf("Dispatcher result for session '%s' was not confirmed; outcome uncertain", e.SessionID)
}

func (e *DispatcherRequestTimeoutError) Unwrap() error { return e.Cause }

// RefusedError is the typed counterpart of the Python FAILED_PRECONDITION
// transport error.
type RefusedError struct {
	Code    codes.Code
	Details string
}

func (e *RefusedError) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Details) }

// ServiceError contains a non-timeout, non-refusal gRPC status returned by an
// Agentplane service.
type ServiceError struct {
	Code    codes.Code
	Details string
	Cause   error
}

func (e *ServiceError) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Details) }

func (e *ServiceError) Unwrap() error { return e.Cause }

func translatedRPCError(err error) error {
	if err == nil {
		return nil
	}
	statusValue, ok := status.FromError(err)
	if !ok {
		return err
	}
	code := statusValue.Code()
	switch code {
	case codes.DeadlineExceeded:
		return &RequestTimeoutError{Cause: err}
	case codes.FailedPrecondition:
		return &RefusedError{Code: code, Details: statusValue.Message()}
	default:
		if code == codes.OK {
			return err
		}
		return &ServiceError{Code: code, Details: statusValue.Message(), Cause: err}
	}
}
