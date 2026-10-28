package worker

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var errBackendOffline = errors.New("backend offline")

// dueInbox is an inbox with three entries, whose notice is prepared if asked, that is due now.
func (h *harness) dueInbox(t *testing.T, prepareNotice bool) store.Subscription {
	t.Helper()
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 3)
	claim := h.claim(t)
	if prepareNotice {
		if notice, err := h.store.PrepareNotice(t.Context(), claim); err != nil || notice == nil {
			t.Fatalf("PrepareNotice = %+v, %v", notice, err)
		}
	}
	if err := h.store.Release(t.Context(), claim, nil, nil); err != nil {
		t.Fatal(err)
	}
	h.makeDue(t, subscription.InboxID)
	return subscription
}

// passDeadline makes the confirmation deadline of a pending retirement, and the inbox's turn, now.
func (h *harness) passDeadline(t *testing.T, inboxID uuid.UUID) {
	t.Helper()
	h.exec(t, `UPDATE inbox SET stale_check_at = now() - interval '1 second', next_attempt = now() - interval '1 second' WHERE id = $1`, inboxID)
}

func requireNotRetired(t *testing.T, h *harness, subscription store.Subscription) inboxRow {
	t.Helper()
	row := h.row(t, subscription.InboxID)
	if row.retired {
		t.Fatal("the inbox was retired")
	}
	if h.subscription(t, subscription.ID).Cancelled {
		t.Fatal("the subscription was cancelled")
	}
	return row
}

func TestStaleDestinationRetiresTheInboxAfterThePersistedRecheck(t *testing.T) {
	for _, reason := range []string{"not_found", "uid_changed"} {
		for _, prepareNotice := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/notice=%v", reason, prepareNotice), func(t *testing.T) {
				h := newHarness(t, debounce(3600, 3600))
				subscription := h.dueInbox(t, prepareNotice)
				inbox := subscription.InboxID
				if reason == "not_found" {
					h.sandboxes.fail(status.Error(codes.NotFound, "sandbox not found"))
				} else {
					replacement := matchingSandbox()
					replacement.Identity.UID = "replacement-uid"
					h.sandboxes.serve(replacement)
				}

				h.step(t)
				pending := requireNotRetired(t, h, subscription)
				if pending.staleCheckAt == nil || pending.nextAttempt == nil || !pending.nextAttempt.Equal(*pending.staleCheckAt) {
					t.Fatalf("pending retirement: stale_check_at=%v next_attempt=%v", pending.staleCheckAt, pending.nextAttempt)
				}
				requireEqual(t, pending.deliveryError, ptr("destination pending retirement: "+reason))
				// A second replica cannot claim the inbox before the confirmation deadline.
				h.requireNoneDue(t)

				h.passDeadline(t, inbox)
				h.restart()
				h.step(t)
				retired := h.row(t, inbox)
				requireEqual(t, retired.retired, true)
				requireEqual(t, retired.nextAttempt, (*time.Time)(nil))
				requireEqual(t, retired.acknowledged, int64(0))
				requireEqual(t, retired.lastCursor, int64(3))
				requireEqual(t, retired.deliveryError, ptr("destination retired: "+reason))
				requireEqual(t, h.subscription(t, subscription.ID).Cancelled, true)

				// What was delivered stays readable, the inbox is never worked again, and no subscription joins it.
				page := h.page(t, inbox)
				requireEqual(t, len(page.Entries), 3)
				requireEqual(t, page.Notice != nil, prepareNotice)
				h.requireNoneDue(t)
				_, err := h.store.Subscribe(t.Context(), testOwner, model.Subscribe{
					DestinationRef: destination(), SessionID: testSession, IdempotencyKey: "new",
					Source: model.ActionsSource{RequestID: uuid.New()}, LifetimeDays: 7,
				}, nil)
				if !errors.Is(err, store.ErrInboxRetired) {
					t.Errorf("Subscribe = %v, want ErrInboxRetired", err)
				}
			})
		}
	}
}

func TestATransientlyMissingDestinationRecoversWithoutRetirement(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.dueInbox(t, false)
	h.sandboxes.fail(status.Error(codes.NotFound, "sandbox not found"))
	h.step(t)
	if h.row(t, subscription.InboxID).staleCheckAt == nil {
		t.Fatal("the missing destination was not noted")
	}

	// After the persisted deadline, the lookup finds the same Sandbox (suspended Sandboxes keep their identity).
	h.sandboxes.serve(matchingSandbox())
	h.makeDue(t, subscription.InboxID)
	h.step(t)
	recovered := requireNotRetired(t, h, subscription)
	requireEqual(t, recovered.staleCheckAt, (*time.Time)(nil))
	requireEqual(t, recovered.deliveryError, (*string)(nil))
}

func TestATransientLookupFailureAndAnOwnerMismatchCannotConfirmStaleness(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.dueInbox(t, false)
	inbox := subscription.InboxID
	h.sandboxes.fail(status.Error(codes.NotFound, "sandbox not found"))
	h.step(t)

	// The deadline has passed, but this lookup fails: staleness is not confirmed, and the recheck waits.
	h.passDeadline(t, inbox)
	h.sandboxes.fail(errBackendOffline)
	before := time.Now()
	h.step(t)
	pending := requireNotRetired(t, h, subscription)
	if pending.staleCheckAt == nil {
		t.Fatal("the pending retirement was forgotten")
	}
	requireWithin(t, "next attempt", pending.nextAttempt, before.Add(5*time.Second), time.Now().Add(5*time.Second))

	// The Sandbox answers, but it is not the owner's: that is not a stale destination either.
	wrongOwner := matchingSandbox()
	wrongOwner.Identity.Name = "different-owner"
	h.sandboxes.serve(wrongOwner)
	h.makeDue(t, inbox)
	h.step(t)
	mismatched := requireNotRetired(t, h, subscription)
	requireEqual(t, mismatched.staleCheckAt, (*time.Time)(nil))
	requireEqual(t, mismatched.deliveryError, ptr("OwnerMismatchError"))
}

func TestAFailedFirstLookupDoesNotStartStaleConfirmation(t *testing.T) {
	for _, c := range []struct {
		name    string
		failure error
		want    string
	}{
		{"unclassified", errBackendOffline, "ServiceError"},
		{"unavailable", status.Error(codes.Unavailable, "refused"), "ServiceError: Unavailable"},
		{"timeout", fmt.Errorf("lookup: %w", context.DeadlineExceeded), "TimeoutError"},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, debounce(3600, 3600))
			subscription := h.dueInbox(t, false)
			h.sandboxes.fail(c.failure)

			h.step(t)

			row := requireNotRetired(t, h, subscription)
			requireEqual(t, row.staleCheckAt, (*time.Time)(nil))
			requireEqual(t, row.deliveryError, &c.want)
		})
	}
}

func TestADestinationOutsideTheServedNamespaceIsRejectedWithoutALookup(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription, err := h.store.Subscribe(t.Context(), testOwner, model.Subscribe{
		DestinationRef: model.DestinationRef{Namespace: "elsewhere", Name: testSandbox, UID: testSandboxUID},
		SessionID:      testSession, IdempotencyKey: "first", Source: model.ActionsSource{RequestID: uuid.New()}, LifetimeDays: 7,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}

	h.step(t)

	row := requireNotRetired(t, h, subscription)
	requireEqual(t, row.deliveryError, ptr("DestinationRejectedError"))
	requireEqual(t, h.sandboxes.lookupCount(), 0)
	requireEqual(t, h.actions.callCount(), 0)
}

func TestAMatchingDestinationIsWorkedAndShowsNoError(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")

	h.step(t)

	row := requireNotRetired(t, h, subscription)
	requireEqual(t, row.deliveryError, (*string)(nil))
	requireEqual(t, h.sandboxes.lookupCount(), 1)
	requireEqual(t, h.actions.callCount(), 1)
}
