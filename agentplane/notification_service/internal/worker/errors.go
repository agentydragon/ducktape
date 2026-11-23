package worker

import (
	"context"
	"errors"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var errOwnerMismatch = errors.New("Session execution Identity does not match the inbox owner")

// unavailableError marks a failed dependency call for a later claim. name is safe to show to the inbox owner.
type unavailableError struct {
	name       string
	err        error
	retryAfter time.Duration
}

func (e *unavailableError) Error() string { return e.name + ": " + e.err.Error() }
func (e *unavailableError) Unwrap() error { return e.err }

// unavailable marks err as a failure to retry. Its name carries the gRPC code, never the message.
func unavailable(err error) error {
	name := "ServiceError"
	switch code := status.Code(err); {
	case errors.Is(err, context.DeadlineExceeded) || code == codes.DeadlineExceeded:
		name = timeoutName
	case code != codes.Unknown:
		name = name + ": " + code.String()
	}
	return &unavailableError{name: name, err: err}
}

// unavailableNotFound gives a missing dependency record an explicit name and a longer retry.
func unavailableNotFound(err error, name string, retryAfter time.Duration) error {
	if status.Code(err) == codes.NotFound {
		return &unavailableError{name: name, err: err, retryAfter: retryAfter}
	}
	return unavailable(err)
}

const timeoutName = "TimeoutError"
