package api

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/client"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
)

// admissionRefused is a caller the authentication refused. Its status is answered as it is.
type admissionRefused struct{ err error }

func (e admissionRefused) Error() string { return e.err.Error() }
func (e admissionRefused) Unwrap() error { return e.err }

// errGitHubDisabled is a GitHub source asked of a service that has no GitHub App.
var errGitHubDisabled = errors.New("GitHub provider is disabled")

// errSandboxMissing is a destination that names no Sandbox the caller may use: it is missing, outside the
// service's namespace, another incarnation, or, for a subscription, another account's.
var errSandboxMissing = errors.New("destination Sandbox not found")

var (
	// errSendDenied is a message to a Sandbox no current grant of the sender covers.
	errSendDenied = errors.New("no session_inbox_access rule bound to this ServiceAccount allows send_message to the Sandbox")
	// errSendPolicyUnavailable is a send while the service's view of the grants is not current: before its first
	// read of them, or once a watch has fallen behind.
	errSendPolicyUnavailable = errors.New("the service's view of send grants is not current")
	// errSessionUnrecorded is a message that would open an inbox for a Session the History Service has not
	// recorded as the destination Sandbox's, which is as unknown to the caller as a missing Sandbox.
	errSessionUnrecorded = errors.New("the History Service records no such Session of the Sandbox")
)

// unavailableError is a collaborator that could not be asked, or answered badly. The client is told only that
// the service could not answer; the cause is logged.
type unavailableError struct {
	what string
	err  error
}

func (e *unavailableError) Error() string { return e.what + ": " + e.err.Error() }
func (e *unavailableError) Unwrap() error { return e.err }

func unavailable(what string, err error) error { return &unavailableError{what: what, err: err} }

// failure answers a failed call with the status code that names it (the codes are those in notifications.proto).
// The message of a failure the client can act on is one this service wrote. A fault of a collaborator or of the
// database gets a fixed message, and its cause is logged where the message names none.
func failure(ctx context.Context, err error) error {
	var (
		refused      admissionRefused
		quota        *store.QuotaError
		limited      *client.RateLimitedError
		providerErr  *worker.ProviderError
		notInstalled *client.NotInstalledError
		suspended    *client.SuspendedError
		access       *client.AccessError
		changed      *client.SourceChangedError
		dependency   *unavailableError
	)
	switch {
	case errors.As(err, &refused):
		return refused.err
	case errors.Is(err, model.ErrInvalid):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.Is(err, store.ErrNotFound), errors.Is(err, errSandboxMissing), errors.Is(err, errNoSession),
		errors.Is(err, errSessionUnrecorded):
		return status.Error(codes.NotFound, store.ErrNotFound.Error())
	case errors.Is(err, store.ErrIdempotencyKeyReused):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.Is(err, store.ErrVersionChanged):
		return status.Error(codes.Aborted, err.Error())
	case errors.Is(err, store.ErrInboxRetired):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, store.ErrCursorOutOfRange):
		return status.Error(codes.OutOfRange, err.Error())
	case errors.As(err, &quota):
		return status.Error(codes.ResourceExhausted, quota.Error())
	case errors.Is(err, errGitHubDisabled), errors.Is(err, errSendDenied):
		return status.Error(codes.PermissionDenied, err.Error())
	case errors.Is(err, errSendPolicyUnavailable):
		return status.Error(codes.Unavailable, err.Error())
	case errors.As(err, &limited):
		return rateLimited(ctx, limited)
	case errors.As(err, &notInstalled):
		return status.Error(codes.PermissionDenied, notInstalled.Error())
	case errors.As(err, &suspended):
		return status.Error(codes.PermissionDenied, suspended.Error())
	case errors.As(err, &access):
		return status.Error(codes.PermissionDenied, access.Error())
	case errors.As(err, &changed):
		return status.Error(codes.PermissionDenied, changed.Error())
	case errors.As(err, &providerErr) && providerErr.Kind == store.FailureAccessDenied:
		return status.Error(codes.PermissionDenied, "Source unavailable or unauthorized")
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "notification request deadline expired")
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case errors.As(err, &dependency):
		slog.WarnContext(ctx, "notification dependency unavailable", "dependency", dependency.what, "error", dependency.err)
		return status.Error(codes.Unavailable, dependency.what+" unavailable")
	default:
		slog.WarnContext(ctx, "notification request failed", "error", err)
		return status.Error(codes.Unavailable, "notifications or a source are temporarily unavailable")
	}
}

// rateLimited is a GitHub rate limit: unavailable, and when to ask again.
func rateLimited(ctx context.Context, limited *client.RateLimitedError) error {
	answer := status.New(codes.Unavailable, "GitHub temporarily unavailable")
	withDetail, err := answer.WithDetails(&errdetails.RetryInfo{
		RetryDelay: durationpb.New(time.Duration(limited.RetrySeconds) * time.Second),
	})
	if err != nil {
		slog.ErrorContext(ctx, "could not attach the retry delay to a rate limit", "error", err)
		return answer.Err()
	}
	return withDetail.Err()
}
