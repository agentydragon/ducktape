package worker

import (
	"context"
	"errors"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var errOwnerMismatch = errors.New("Session execution Identity does not match the inbox owner")

// unavailableError marks a failed dependency call for a later claim. name is safe to show to the inbox owner.
type unavailableError struct {
	name string
	err  error
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

const timeoutName = "TimeoutError"
