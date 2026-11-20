package worker

import (
	"context"
	"testing"
	"time"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestMissingSessionKeepsInboxAndUsesLongRetry(t *testing.T) {
	settings := debounce(0, 300)
	h := newHarness(t, settings)
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 1)
	h.dispatcher.getErr = status.Error(codes.NotFound, "session not found")
	before := time.Now()

	h.step(t)

	after := time.Now()
	row := h.row(t, subscription.InboxID)
	requireEqual(t, row.deliveryError, ptr("SessionNotFoundError"))
	if row.retired || row.acknowledged != 0 || row.lastCursor != 1 {
		t.Fatalf("inbox state = retired %t, acknowledged %d, last cursor %d; want retained data", row.retired, row.acknowledged, row.lastCursor)
	}
	retry := seconds(settings.UnavailableDestinationRetrySeconds)
	requireWithin(t, "retry", row.nextAttempt, before.Add(retry), after.Add(retry))
	requireSubmitted(t, h)
	h.requireNoneDue(t)
}

func TestMissingHistoryKeepsPendingNoticeAndUsesLongRetry(t *testing.T) {
	settings := debounce(0, 300)
	h := newHarness(t, settings)
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 1)
	h.history.err = status.Error(codes.NotFound, "session history not found")
	before := time.Now()

	h.step(t)

	after := time.Now()
	row := h.row(t, subscription.InboxID)
	requireEqual(t, row.deliveryError, ptr("HistoryNotFoundError"))
	if row.retired || row.acknowledged != 0 || row.lastCursor != 1 {
		t.Fatalf("inbox state = retired %t, acknowledged %d, last cursor %d; want retained data", row.retired, row.acknowledged, row.lastCursor)
	}
	notice := h.notice(t, subscription.InboxID)
	if notice.Attempted || notice.Confirmed || notice.Error != nil {
		t.Fatalf("notice = %+v, want pending and unattempted", notice)
	}
	retry := seconds(settings.UnavailableDestinationRetrySeconds)
	requireWithin(t, "retry", row.nextAttempt, before.Add(retry), after.Add(retry))
	requireSubmitted(t, h)
	h.requireNoneDue(t)
}

func TestSubmitInputNotFoundKeepsPendingNoticeAndUsesLongRetry(t *testing.T) {
	settings := debounce(0, 300)
	h := newHarness(t, settings)
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 1)
	h.dispatcher.respond = func(context.Context, *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
		return nil, status.Error(codes.NotFound, "sandbox not found")
	}
	before := time.Now()

	h.step(t)

	after := time.Now()
	row := h.row(t, subscription.InboxID)
	requireEqual(t, row.deliveryError, ptr("DispatcherNotFoundError"))
	if row.retired || row.acknowledged != 0 || row.lastCursor != 1 {
		t.Fatalf("inbox state = retired %t, acknowledged %d, last cursor %d; want retained data", row.retired, row.acknowledged, row.lastCursor)
	}
	notice := h.notice(t, subscription.InboxID)
	if !notice.Attempted || notice.Confirmed || notice.Error != nil {
		t.Fatalf("notice = %+v, want attempted, pending and unconfirmed", notice)
	}
	retry := seconds(settings.UnavailableDestinationRetrySeconds)
	requireWithin(t, "retry", row.nextAttempt, before.Add(retry), after.Add(retry))
	requireSubmitted(t, h, submissionOf(notice))
	h.requireNoneDue(t)
}
