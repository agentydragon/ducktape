package worker

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
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

// passDeadline makes the unavailable destination recheck and the inbox's turn due.
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

func TestStaleDestinationKeepsTheInboxAndSourcesWithoutDeliveringToAReplacement(t *testing.T) {
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
					replacement.Identity.Uid = "replacement-uid"
					h.sandboxes.serve(replacement)
				}

				h.step(t)
				pending := requireNotRetired(t, h, subscription)
				if pending.staleCheckAt == nil || pending.nextAttempt == nil || !pending.nextAttempt.Equal(*pending.staleCheckAt) {
					t.Fatalf("destination recheck: stale_check_at=%v next_attempt=%v", pending.staleCheckAt, pending.nextAttempt)
				}
				requireEqual(t, pending.deliveryError, ptr("destination unavailable: "+reason))
				// An existing notice remains, and the absent or replacement Sandbox never receives it.
				requireEqual(t, h.page(t, inbox).Notice != nil, prepareNotice)
				requireEqual(t, len(h.dispatcher.submitted()), 0)
				// A second replica cannot claim the inbox before the recheck deadline.
				h.requireNoneDue(t)

				h.passDeadline(t, inbox)
				h.restart()
				h.step(t)
				stillMissing := requireNotRetired(t, h, subscription)
				requireEqual(t, stillMissing.acknowledged, int64(0))
				requireEqual(t, stillMissing.lastCursor, int64(3))
				requireEqual(t, stillMissing.deliveryError, ptr("destination unavailable: "+reason))
				requireEqual(t, len(h.dispatcher.submitted()), 0)
				// Existing entries and the immutable notice survive the second check and a worker restart.
				page := h.page(t, inbox)
				requireEqual(t, len(page.Entries), 3)
				requireEqual(t, page.Notice != nil, prepareNotice)
				h.requireNoneDue(t)
			})
		}
	}
}

func TestMissingDestinationContinuesSourceReadsAndPreparesANotice(t *testing.T) {
	h := newHarness(t, debounce(0, 10))
	subscription := h.subscribe(t, "first")
	h.addEvents(t, subscription, 1, 1)
	h.sandboxes.fail(status.Error(codes.NotFound, "sandbox not found"))

	h.step(t)
	requireEqual(t, h.actions.callCount(), 1)
	requireEqual(t, h.page(t, subscription.InboxID).Inbox.LastCursor, int64(1))
	prepared := h.notice(t, subscription.InboxID)
	requireEqual(t, prepared.ThroughCursor, int64(1))
	requireEqual(t, prepared.Attempted, false)
	requireEqual(t, len(h.dispatcher.submitted()), 0)

	// New source entries continue to accumulate while the first notice waits for its exact target.
	h.addEvents(t, subscription, 2, 2)
	h.makeDue(t, subscription.InboxID)
	h.restart()
	h.step(t)
	requireEqual(t, h.page(t, subscription.InboxID).Inbox.LastCursor, int64(2))
	requireEqual(t, h.notice(t, subscription.InboxID).CommandID, prepared.CommandID)
	requireEqual(t, h.notice(t, subscription.InboxID).Text, prepared.Text)
	requireEqual(t, len(h.dispatcher.submitted()), 0)
	requireNotRetired(t, h, subscription)

	h.sandboxes.serve(matchingSandbox())
	h.makeDue(t, subscription.InboxID)
	h.step(t)
	submitted := h.dispatcher.submitted()
	requireEqual(t, len(submitted), 1)
	requireEqual(t, submitted[0].GetExpectedSandboxUid(), testSandboxUID)
	requireEqual(t, submitted[0].GetText(), prepared.Text)
}

func TestAMissingDestinationRecoversWithoutRetirement(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.dueInbox(t, false)
	h.sandboxes.fail(status.Error(codes.NotFound, "sandbox not found"))
	h.step(t)
	if h.row(t, subscription.InboxID).staleCheckAt == nil {
		t.Fatal("the missing destination was not noted")
	}

	// The same UID can resume delivery after the persisted recheck deadline.
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

	// The deadline has passed, but this lookup fails: the recheck waits.
	h.passDeadline(t, inbox)
	h.sandboxes.fail(errBackendOffline)
	before := time.Now()
	h.step(t)
	pending := requireNotRetired(t, h, subscription)
	if pending.staleCheckAt == nil {
		t.Fatal("the unavailable destination was forgotten")
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
	}, nil, "test-account-uid")
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

func TestWorkerDoesNotReadSourcesForUnknownOrChangedSessionOwner(t *testing.T) {
	for _, uid := range []string{"", "replacement-account-uid"} {
		t.Run(uid, func(t *testing.T) {
			h := newHarness(t, debounce(0, 0))
			subscription := h.subscribe(t, "owner-uid")
			h.dispatcher.accountUID = uid
			h.step(t)
			if h.actions.callCount() != 0 || len(h.dispatcher.submitted()) != 0 {
				t.Fatal("worked sources or delivered under an unproven owner")
			}
			requireNotRetired(t, h, subscription)
		})
	}
}

func TestWorkerEstablishesOwnerBeforeItsFirstSourceRead(t *testing.T) {
	h := newHarness(t, debounce(0, 0))
	subscription := h.subscribe(t, "establish-before-source")
	h.exec(t, `UPDATE inbox SET owner_uid = NULL WHERE id = $1`, subscription.InboxID)
	seen := false
	h.actions.on(func(_ context.Context, claim store.Claim, _ store.DueSubscription) error {
		seen = true
		if claim.OwnerUID == nil || *claim.OwnerUID != "test-account-uid" {
			t.Fatal("source read without the established owner UID")
		}
		return nil
	})
	h.step(t)
	if !seen {
		t.Fatal("source was not reconciled after proving its owner")
	}
	inbox := h.page(t, subscription.InboxID).Inbox
	if inbox.OwnerUID == nil || *inbox.OwnerUID != "test-account-uid" {
		t.Fatalf("owner UID was not persisted: %v", inbox.OwnerUID)
	}
}

// An inbox whose Session the Dispatcher recorded as executing as an Identity alone, its Sandbox having
// no ServiceAccount, is that Identity incarnation's: its notices are delivered, and a same-named
// successor recorded for the Session is an owner mismatch.
func TestAnIdentityRunSessionsInboxIsDeliveredAndPinnedToItsIdentity(t *testing.T) {
	h := newHarness(t, debounce(0, 300))
	identity := model.Identity{Name: "test-identity", UID: "test-identity-uid"}
	h.dispatcher.accountUID = ""
	h.dispatcher.identity = &identitypb.Identity{Name: identity.Name, Uid: identity.UID}
	subscription, err := h.store.SubscribeAsIdentity(t.Context(), identity, model.Subscribe{
		DestinationRef: destination(), SessionID: testSession, IdempotencyKey: "first",
		Source: model.ActionsSource{RequestID: uuid.New()}, LifetimeDays: 7,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 2)

	h.step(t)
	page, err := h.store.Read(t.Context(), store.IdentityOwner{Identity: identity, SandboxUID: testSandboxUID}, subscription.InboxID, 0, 128)
	if err != nil || page.Notice == nil || !page.Notice.Attempted || len(h.dispatcher.submitted()) != 1 {
		t.Fatalf("the Identity's inbox %+v (%v), submitted %d; want one attempted notice", page, err, len(h.dispatcher.submitted()))
	}

	h.dispatcher.identity = &identitypb.Identity{Name: identity.Name, Uid: "test-successor-uid"}
	h.addEvents(t, subscription, 3, 3)
	h.makeDue(t, subscription.InboxID)
	h.step(t)
	if row := h.row(t, subscription.InboxID); row.deliveryError == nil || *row.deliveryError != "OwnerMismatchError" {
		t.Fatalf("delivery error %v, want OwnerMismatchError", row.deliveryError)
	}
	requireEqual(t, len(h.dispatcher.submitted()), 1)
}
