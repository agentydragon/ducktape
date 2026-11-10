package main

import (
	"context"
	"errors"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/app/go/httperror"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/danielgtaylor/huma/v2"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

func sandboxHTTPError(err error) error {
	return httperror.LogServerError(classifySandboxError(err), err)
}

func classifySandboxError(err error) error {
	var quiescing *sandboxes.SandboxQuiescingError
	switch {
	case apierrors.IsNotFound(err):
		return huma.Error404NotFound(err.Error())
	default:
		if errors.As(err, &quiescing) {
			return huma.Error503ServiceUnavailable(err.Error())
		}
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
