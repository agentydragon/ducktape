package worker

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Sandboxes reads the Sandbox a destination names by name.
type Sandboxes interface {
	Get(ctx context.Context, name string) (sandboxreader.View, error)
}

var (
	// errDestinationRejected is a destination outside the namespace the service serves.
	errDestinationRejected = errors.New("destination is outside the service's namespace")
	// errOwnerMismatch is a destination whose Sandbox does not run as the inbox's owner. Unlike a stale
	// destination it never counts towards retiring the inbox.
	errOwnerMismatch = errors.New("destination Sandbox does not run as the inbox's owner")
)

// staleDestinationError is a destination that no longer names the Sandbox it did: that Sandbox is gone, or
// a new one has its name.
type staleDestinationError struct{ reason string }

const (
	staleNotFound   = "not_found"
	staleUIDChanged = "uid_changed"
)

func (e *staleDestinationError) Error() string { return "stale destination: " + e.reason }

// unavailableError is a failure of something the worker calls, which a later claim retries. name is what the
// inbox shows as its delivery error.
type unavailableError struct {
	name string
	err  error
}

func (e *unavailableError) Error() string { return e.name + ": " + e.err.Error() }
func (e *unavailableError) Unwrap() error { return e.err }

// unavailable marks err as a failure to retry. The name carries the gRPC code, which is safe to show, and never
// the message.
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

// checkDestination resolves the inbox's destination to the exact Sandbox incarnation it names, which must run
// as the inbox's owner.
func (w *Worker) checkDestination(ctx context.Context, claim store.Claim) error {
	destination := claim.DestinationRef
	if destination.Namespace != w.Namespace {
		return errDestinationRejected
	}
	view, err := w.Sandboxes.Get(ctx, destination.Name)
	switch {
	case status.Code(err) == codes.NotFound:
		return &staleDestinationError{staleNotFound}
	case err != nil:
		return unavailable(err)
	case view.Identity.UID != destination.UID:
		return &staleDestinationError{staleUIDChanged}
	}
	// A Sandbox runs as the ServiceAccount of its own name.
	if (store.ServiceAccount{Namespace: view.Identity.Namespace, Name: view.Identity.Name}) != claim.Owner {
		return errOwnerMismatch
	}
	return nil
}
