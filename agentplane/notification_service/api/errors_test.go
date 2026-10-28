package api

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestFailureAnswersEachTypedErrorWithTheStatusCodeTheProtoNames(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want codes.Code
	}{
		{"invalid request", fmt.Errorf("%w: limit must be 1 to 128", model.ErrInvalid), codes.InvalidArgument},
		{"missing or foreign resource", store.ErrNotFound, codes.NotFound},
		{"wrapped missing resource", fmt.Errorf("renew: %w", store.ErrNotFound), codes.NotFound},
		{"destination Sandbox", errSandboxMissing, codes.NotFound},
		{"Session the runner lacks", errNoSession, codes.NotFound},
		{"reused key", store.ErrIdempotencyKeyReused, codes.AlreadyExists},
		{"moved version", store.ErrVersionChanged, codes.Aborted},
		{"retired inbox", store.ErrInboxRetired, codes.FailedPrecondition},
		{"cursor beyond the inbox", store.ErrCursorOutOfRange, codes.OutOfRange},
		{"quota", &store.QuotaError{Quota: store.InboxesPerAccount, Limit: 64}, codes.ResourceExhausted},
		{"GitHub provider off", errGitHubDisabled, codes.PermissionDenied},
		{"source denied", &worker.ProviderError{Kind: store.FailureAccessDenied, Message: "denied"}, codes.PermissionDenied},
		{"source throttled", &worker.ProviderError{Kind: store.FailureRateLimited, Message: "throttled"}, codes.Unavailable},
		{"collaborator down", unavailable("runner", errors.New("dial secret-host")), codes.Unavailable},
		{"database fault", errors.New("pq: password authentication failed for secret-user"), codes.Unavailable},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			answer := failure(t.Context(), c.err)
			if got := status.Code(answer); got != c.want {
				t.Errorf("code = %v (%v), want %v", got, answer, c.want)
			}
			if strings.Contains(status.Convert(answer).Message(), "secret") {
				t.Errorf("the client was told %q", status.Convert(answer).Message())
			}
		})
	}
}

func TestFailurePassesAnAdmissionRefusalOn(t *testing.T) {
	refusal := status.Error(codes.Unauthenticated, "invalid bearer")
	if answer := failure(t.Context(), admissionRefused{refusal}); answer != refusal {
		t.Errorf("answer = %v, want %v", answer, refusal)
	}
}

func TestFailureNamesAnEndedContextWhateverFailedBecauseOfIt(t *testing.T) {
	expired, cancelExpired := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancelExpired()
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	cause := unavailable("runner", context.Cause(expired))
	if got := status.Code(failure(expired, cause)); got != codes.DeadlineExceeded {
		t.Errorf("an expired call = %v, want DeadlineExceeded", got)
	}
	if got := status.Code(failure(cancelled, cause)); got != codes.Canceled {
		t.Errorf("a cancelled call = %v, want Canceled", got)
	}
}
