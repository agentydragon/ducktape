package github

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/client"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
)

// refreshFailed records a failed refresh on its shared row and returns the deferral that ends the reconcile until
// the row may be refreshed again. An error that is none of the client's typed failures is returned as it is, and
// the lease runs out.
func (p *Provider) refreshFailed(ctx context.Context, lease store.GitHubRefreshLease, cause error) error {
	failure, ok := classify(cause)
	if !ok {
		return cause
	}
	until, err := p.state.Fail(ctx, lease, failure.kind, failure.message, failure.retry)
	if err != nil {
		return err
	}
	slog.WarnContext(ctx, "GitHub shared refresh failed", "key", lease.Key, "cause", failure.message,
		"retry_seconds", failure.retry.Seconds())
	return &store.RefreshDeferredError{Until: until}
}

// refreshFailure is how a refresh's error shows on the shared row: the message is safe to show, never an upstream
// body or a token.
type refreshFailure struct {
	kind    store.SourceFailureKind
	message string
	retry   time.Duration
}

// classify sorts the client's typed errors, and reports false for any other error: a caller that went away, or a
// bug.
func classify(err error) (refreshFailure, bool) {
	var limited *client.RateLimitedError
	var denied *client.AccessError
	var changed *client.SourceChangedError
	var status *client.StatusError
	var transport *client.TransportError
	var invalid *client.InvalidResponseError
	switch {
	case errors.As(err, &limited):
		return refreshFailure{store.FailureRateLimited, err.Error(), time.Duration(limited.RetrySeconds) * time.Second}, true
	case errors.As(err, &denied):
		return refreshFailure{store.FailureAccessDenied, err.Error(), worker.DefaultRetry}, true
	case errors.As(err, &changed):
		return refreshFailure{store.FailureSourceChanged, err.Error(), worker.DefaultRetry}, true
	case errors.As(err, &status):
		kind := store.FailureProcessingError
		if status.StatusCode >= 500 {
			kind = store.FailureUnavailable
		}
		return refreshFailure{kind, err.Error(), worker.DefaultRetry}, true
	case errors.As(err, &transport):
		return refreshFailure{store.FailureUnavailable, "GitHub request failed", worker.DefaultRetry}, true
	case errors.As(err, &invalid):
		return refreshFailure{store.FailureProcessingError, "GitHub returned an invalid response", worker.DefaultRetry}, true
	}
	return refreshFailure{}, false
}
