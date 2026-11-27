package sessionclient

import (
	"fmt"

	"github.com/google/uuid"
)

// MalformedMessageError means request fields are not valid proto-JSON for the
// expected message.
type MalformedMessageError struct{ Cause error }

func (e *MalformedMessageError) Error() string {
	return fmt.Sprintf("not a SessionSpec: %v", e.Cause)
}

func (e *MalformedMessageError) Unwrap() error { return e.Cause }

// SessionNotFoundError hides an session that belongs to another
// Sandbox, or whose returned identity does not match the requested Session.
type SessionNotFoundError struct {
	SessionID uuid.UUID
	Sandbox   string
}

func (e *SessionNotFoundError) Error() string {
	return fmt.Sprintf("Session %s was not found in Sandbox %q", e.SessionID, e.Sandbox)
}
