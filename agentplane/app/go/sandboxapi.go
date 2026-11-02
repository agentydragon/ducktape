package main

import (
	"context"
	"errors"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/app/go/httperror"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/management"
	"github.com/danielgtaylor/huma/v2"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

func sandboxHTTPError(err error) error {
	return httperror.LogServerError(classifySandboxError(err), err)
}

func classifySandboxError(err error) error {
	var notFound *management.SandboxNotFoundError
	var invalid *management.InvalidSandboxError
	var conflict *management.SandboxConflictError
	var running *management.SandboxRunningError
	var modeUnknown *management.SandboxModeUnknownError
	var tearingDown *management.SandboxTearingDownError
	var quiescing *management.SandboxQuiescingError
	switch {
	case errors.As(err, &notFound), apierrors.IsNotFound(err):
		return huma.Error404NotFound(err.Error())
	case errors.As(err, &invalid):
		return huma.Error422UnprocessableEntity(err.Error())
	case errors.As(err, &conflict), errors.As(err, &running), errors.As(err, &modeUnknown), errors.As(err, &tearingDown):
		return huma.Error409Conflict(err.Error())
	case errors.As(err, &quiescing):
		return huma.Error503ServiceUnavailable(err.Error())
	default:
		switch grpcstatus.Code(err) {
		case codes.NotFound:
			return huma.Error404NotFound(err.Error())
		case codes.InvalidArgument:
			return huma.Error422UnprocessableEntity(err.Error())
		case codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted:
			return huma.Error409Conflict(err.Error())
		case codes.ResourceExhausted:
			return huma.Error429TooManyRequests(err.Error())
		case codes.DeadlineExceeded:
			return huma.Error504GatewayTimeout("Upstream deadline expired")
		}
		if apierrors.IsConflict(err) {
			return huma.Error409Conflict(err.Error())
		}
		if errors.Is(err, context.DeadlineExceeded) || strings.Contains(strings.ToLower(err.Error()), "timeout") {
			return huma.Error504GatewayTimeout("Upstream deadline expired")
		}
		return huma.Error503ServiceUnavailable("Upstream service unavailable")
	}
}

func quote(value string) string { return "'" + strings.ReplaceAll(value, "'", "\\'") + "'" }
