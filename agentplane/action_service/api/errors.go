package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// errStoredRequest is a stored request no message can show: a fault of the service, not of the caller.
var errStoredRequest = errors.New("stored action request cannot be shown")

func invalidRequest(format string, args ...any) error {
	return fmt.Errorf("%w: %s", store.ErrInvalid, fmt.Sprintf(format, args...))
}

// conflictCodes are the codes action_service.proto and operator_action_service.proto name for each conflict a
// call can meet.
var conflictCodes = map[store.ConflictReason]codes.Code{
	store.ConflictIdempotencyKeyUsed: codes.AlreadyExists,
	store.ConflictDecisionKeyReused:  codes.AlreadyExists,
	store.ConflictVersionChanged:     codes.Aborted,
	store.ConflictAlreadyDecided:     codes.FailedPrecondition,
	store.ConflictNoPendingDispatch:  codes.FailedPrecondition,
}

// failure answers a failed call with the status code the protos name for it. A message the client can act on is
// one this service wrote; a fault of the database or of a collaborator gets a fixed message, and its cause is
// logged.
func failure(ctx context.Context, err error) error {
	var conflict *store.ConflictError
	switch {
	case err == nil:
		return nil
	case status.Code(err) != codes.Unknown:
		// An admission refusal, or a failure already answered.
		return err
	case errors.Is(err, store.ErrNotFound):
		return status.Error(codes.NotFound, store.ErrNotFound.Error())
	case errors.Is(err, store.ErrInvalid):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.As(err, &conflict) && conflictCodes[conflict.Reason] != codes.OK:
		return status.Error(conflictCodes[conflict.Reason], conflict.Message)
	case errors.Is(err, errDraining):
		return status.Error(codes.Unavailable, errDraining.Error())
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "action request deadline expired")
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case errors.Is(err, auth.ErrUnavailable):
		slog.WarnContext(ctx, "workload authentication unavailable", "error", err)
		return status.Error(codes.Unavailable, auth.ErrUnavailable.Error())
	case errors.Is(err, errStoredRequest):
		slog.ErrorContext(ctx, "stored action request cannot be shown", "error", err)
		return status.Error(codes.Internal, errStoredRequest.Error())
	default:
		slog.WarnContext(ctx, "action request failed", "error", err)
		return status.Error(codes.Unavailable, "the Action Service is temporarily unavailable")
	}
}
