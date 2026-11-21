package worker

import (
	"context"
	"testing"
	"time"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// A NotFound from a dependency keeps the inbox's data and its pending notice, and retries after the long interval.
func TestNotFoundFromADependencyKeepsInboxAndUsesLongRetry(t *testing.T) {
	for _, c := range []struct {
		name          string
		fail          func(h *harness)
		wantError     string
		wantAttempted *bool // whether the pending notice was submitted before the failure; nil when no notice is inspected
	}{
		{"Session", func(h *harness) { h.dispatcher.getErr = status.Error(codes.NotFound, "session not found") }, "SessionNotFoundError", nil},
		{"History", func(h *harness) { h.history.err = status.Error(codes.NotFound, "session history not found") }, "HistoryNotFoundError", ptr(false)},
		{"SubmitInput", func(h *harness) {
			h.dispatcher.respond = func(context.Context, *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
				return nil, status.Error(codes.NotFound, "sandbox not found")
			}
		}, "DispatcherNotFoundError", ptr(true)},
	} {
		t.Run(c.name, func(t *testing.T) {
			settings := debounce(0, 300)
			h := newHarness(t, settings)
			subscription := h.subscribe(t, "first")
			h.postponeSources(t)
			h.addEvents(t, subscription, 1, 1)
			c.fail(h)
			before := time.Now()

			h.step(t)

			after := time.Now()
			row := h.row(t, subscription.InboxID)
			requireEqual(t, row.deliveryError, ptr(c.wantError))
			if row.retired || row.acknowledged != 0 || row.lastCursor != 1 {
				t.Fatalf("inbox state = retired %t, acknowledged %d, last cursor %d; want retained data", row.retired, row.acknowledged, row.lastCursor)
			}
			retry := seconds(settings.UnavailableDestinationRetrySeconds)
			requireWithin(t, "retry", row.nextAttempt, before.Add(retry), after.Add(retry))
			var submitted []*dispatcherpb.SubmitInputRequest
			if c.wantAttempted != nil {
				notice := h.notice(t, subscription.InboxID)
				if notice.Attempted != *c.wantAttempted || notice.Confirmed || notice.Error != nil {
					t.Fatalf("notice = %+v, want pending and unconfirmed, attempted %t", notice, *c.wantAttempted)
				}
				if notice.Attempted {
					submitted = append(submitted, submissionOf(notice))
				}
			}
			requireSubmitted(t, h, submitted...)
			h.requireNoneDue(t)
		})
	}
}
