package github

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
)

func TestARefreshFailureIsRecordedUnderItsKindAndRetryDelay(t *testing.T) {
	for _, tc := range []struct {
		name  string
		err   error
		kind  store.SourceFailureKind
		retry time.Duration
	}{
		{"rate limit", &RateLimitedError{StatusCode: 403, RetrySeconds: 120}, store.FailureRateLimited, 120 * time.Second},
		{"secondary rate limit", fmt.Errorf("reading: %w", &RateLimitedError{StatusCode: 429, RetrySeconds: 7}), store.FailureRateLimited, 7 * time.Second},
		{"access", &AccessError{StatusCode: 403}, store.FailureAccessDenied, worker.DefaultRetry},
		{"uninstalled", &NotInstalledError{}, store.FailureAccessDenied, worker.DefaultRetry},
		{"suspended", &SuspendedError{}, store.FailureAccessDenied, worker.DefaultRetry},
		{"source changed", &SourceChangedError{Reason: "identity changed"}, store.FailureSourceChanged, worker.DefaultRetry},
		{"server error", &StatusError{StatusCode: 502}, store.FailureUnavailable, worker.DefaultRetry},
		{"unexpected status", &StatusError{StatusCode: 302}, store.FailureProcessingError, worker.DefaultRetry},
		{"transport", &TransportError{Err: errors.New("dial tcp")}, store.FailureUnavailable, worker.DefaultRetry},
		{"invalid response", &invalidResponseError{reason: "no id"}, store.FailureProcessingError, worker.DefaultRetry},
	} {
		t.Run(tc.name, func(t *testing.T) {
			failure, ok := classify(tc.err)
			if !ok || failure.kind != tc.kind || failure.retry != tc.retry || failure.message == "" {
				t.Fatalf("classify(%v) = %+v, %v; want kind %s retrying in %s", tc.err, failure, ok, tc.kind, tc.retry)
			}
		})
	}
}

// The message is shown to the subscriber: it never carries what a transport or a response body said.
func TestARefreshFailureMessageDoesNotRepeatTheUpstreamCause(t *testing.T) {
	for _, err := range []error{
		&TransportError{Err: errors.New(`Get "https://api.github.test/?token=sekrit": refused`)},
		&invalidResponseError{reason: "sekrit"},
	} {
		failure, ok := classify(err)
		if !ok || strings.Contains(failure.message, "sekrit") {
			t.Fatalf("classify(%v) = %+v, %v; want a message without the cause", err, failure, ok)
		}
	}
}

// Anything else, such as a caller that went away or a bug, is not a statement about GitHub: it is not recorded on
// a shared row.
func TestAnErrorThatIsNotGitHubsIsNotClassified(t *testing.T) {
	for _, err := range []error{context.Canceled, context.DeadlineExceeded, errors.New("bug"), nil} {
		if failure, ok := classify(err); ok {
			t.Fatalf("classify(%v) = %+v; want it unclassified", err, failure)
		}
	}
}
