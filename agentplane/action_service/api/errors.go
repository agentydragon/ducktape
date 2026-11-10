package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/push"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
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
}

// failure answers a failed call with the status code the protos name for it. A message the client can act on is
// one this service wrote; a fault of the database or of a collaborator gets a fixed message, and its cause is
// logged.
func failure(ctx context.Context, err error) error {
	var (
		conflict *store.ConflictError
		unusable *linkage.UnusableError
	)
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
	case errors.Is(err, catalog.ErrUnknownAction):
		return status.Error(codes.NotFound, err.Error())
	case errors.Is(err, catalog.ErrUnavailable):
		return status.Error(codes.Unavailable, catalog.ErrUnavailable.Error())
	case errors.Is(err, linkage.ErrUnknownServer):
		return status.Error(codes.NotFound, linkage.ErrUnknownServer.Error())
	case errors.Is(err, linkage.ErrUnconfiguredScopes):
		return status.Error(codes.InvalidArgument, linkage.ErrUnconfiguredScopes.Error())
	case errors.As(err, &conflict) && conflictCodes[conflict.Reason] != codes.OK:
		return status.Error(conflictCodes[conflict.Reason], conflict.Message)
	case errors.Is(err, push.ErrUnsupportedEndpoint):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.Is(err, push.ErrNotFound):
		return status.Error(codes.NotFound, err.Error())
	case errors.Is(err, push.ErrRegisteredElsewhere):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.Is(err, errPushNotConfigured):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, errDraining):
		return status.Error(codes.Unavailable, errDraining.Error())
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "action request deadline expired")
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case errors.As(err, &unusable):
		// After the call's own end: discovery fails once the call is cancelled, and that is not the server's fault.
		return status.Error(codes.FailedPrecondition, unusable.Reason)
	case errors.Is(err, auth.ErrUnavailable):
		slog.WarnContext(ctx, "workload authentication unavailable", "error", err)
		return status.Error(codes.Unavailable, auth.ErrUnavailable.Error())
	case errors.Is(err, errStoredRequest):
		slog.ErrorContext(ctx, "stored action request cannot be shown", "error", err)
		return status.Error(codes.Internal, errStoredRequest.Error())
	case errors.Is(err, errCatalogAction):
		slog.ErrorContext(ctx, "catalog Action cannot be shown", "error", err)
		return status.Error(codes.Internal, errCatalogAction.Error())
	default:
		slog.WarnContext(ctx, "action request failed", "error", err)
		return status.Error(codes.Unavailable, "the Action Service is temporarily unavailable")
	}
}
