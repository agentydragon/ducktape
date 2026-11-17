package worker

import (
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

func TestPlacedSessionNoticeIsSubmittedWithoutSandboxUID(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.dueInbox(t, true)

	h.step(t)

	notice := h.notice(t, subscription.InboxID)
	requireSubmitted(t, h, submissionOf(notice))
	if submitted := h.dispatcher.submitted()[0]; submitted.GetExpectedSandboxUid() != "" {
		t.Fatalf("ExpectedSandboxUid = %q, want empty", submitted.GetExpectedSandboxUid())
	}
	if row := h.row(t, subscription.InboxID); row.deliveryError != nil {
		t.Fatalf("delivery error = %v, want none", *row.deliveryError)
	}
}

func TestUnplacedSessionKeepsItsPendingNoticeAndExistingRetryOutcome(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.dueInbox(t, true)
	h.dispatcher.placement = nil
	before := time.Now()

	h.step(t)

	row := requireNotRetired(t, h, subscription)
	requireEqual(t, row.deliveryError, ptr("OwnerMismatchError"))
	if row.acknowledged != 0 || row.lastCursor != 3 {
		t.Fatalf("inbox cursors = acknowledged %d, last %d; want 0 and 3", row.acknowledged, row.lastCursor)
	}
	requireWithin(t, "retry", row.nextAttempt, before.Add(5*time.Second), time.Now().Add(5*time.Second))
	if notice := h.notice(t, subscription.InboxID); notice.Attempted || notice.Confirmed {
		t.Fatalf("pending notice = %+v; want unattempted and unconfirmed", notice)
	}
	requireEqual(t, len(h.dispatcher.submitted()), 0)
	h.requireNoneDue(t)
}

func TestSessionIdentityChangeStillDeniesNoticeDelivery(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.dueInbox(t, true)
	h.dispatcher.identity = &identitypb.Identity{Name: testOwner.Name, Uid: "replacement-identity-uid"}

	h.step(t)

	row := requireNotRetired(t, h, subscription)
	requireEqual(t, row.deliveryError, ptr("OwnerMismatchError"))
	requireEqual(t, len(h.dispatcher.submitted()), 0)
}
