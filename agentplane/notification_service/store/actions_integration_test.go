package store

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
)

// eventsThrough is the events of an Action request with sequences first through last.
func eventsThrough(first, last int64) []ActionEvent {
	var events []ActionEvent
	for sequence := first; sequence <= last; sequence++ {
		events = append(events, ActionEvent{
			Sequence: sequence, State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, At: time.Date(2026, 10, 20, 3, 56, 3, 0, time.UTC),
		})
	}
	return events
}

// dueOf is the subscription as a read of its source finds it, before anything is recorded.
func dueOf(subscription Subscription) DueSubscription {
	return DueSubscription{Subscription: subscription, ActionsAfterSequence: ptr(int64(0))}
}

func (f fixture) record(claim Claim, subscription Subscription, events []ActionEvent) error {
	return f.RecordActionEvents(context.Background(), claim, dueOf(subscription), events)
}

func (f fixture) mustRecord(t *testing.T, claim Claim, subscription Subscription, events []ActionEvent) {
	t.Helper()
	if err := f.record(claim, subscription, events); err != nil {
		t.Fatal(err)
	}
}

// requireEntries requires the inbox to hold exactly the events of the request with these sequences, in order.
func (f fixture) requireEntries(t *testing.T, inboxID, requestID uuid.UUID, sequences ...int64) {
	t.Helper()
	page := f.read(t, inboxID, 0)
	var got []int64
	for i, entry := range page.Entries {
		event, ok := entry.Event.(ActionsEvent)
		if !ok || event.RequestID != requestID {
			t.Fatalf("entry %d is %+v, want an event of request %s", entry.Cursor, entry.Event, requestID)
		}
		got = append(got, event.Sequence)
		if entry.Cursor != int64(i+1) {
			t.Errorf("entry %d has cursor %d", i, entry.Cursor)
		}
	}
	requireEqual(t, got, sequences)
}

func (f fixture) nextAttempt(t *testing.T, id uuid.UUID) time.Time {
	t.Helper()
	var next time.Time
	if err := f.db.QueryRowContext(t.Context(), `SELECT next_attempt FROM subscription WHERE id = $1`, id).Scan(&next); err != nil {
		t.Fatal(err)
	}
	return next
}

func (f fixture) actionsAfterSequence(t *testing.T, id uuid.UUID) int64 {
	t.Helper()
	var sequence int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT actions_after_sequence FROM subscription WHERE id = $1`, id).Scan(&sequence); err != nil {
		t.Fatal(err)
	}
	return sequence
}

func TestRecordActionEventsAppendsTheNewEventsAndMovesTheSubscriptionOn(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, body("first"))
	request := subscription.Source.(ActionsSource).RequestID
	claim := f.claim(t)
	due := f.source(t, claim)
	if due == nil {
		t.Fatal("the new subscription is not due")
	}
	events := eventsThrough(1, 3)
	events[2].Actor = &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: action.ServiceAccount{Namespace: "test-namespace", Name: "test-sandbox"}.Proto()}}

	before := time.Now()
	if err := f.RecordActionEvents(t.Context(), claim, *due, events); err != nil {
		t.Fatal(err)
	}
	after := time.Now()

	f.requireEntries(t, subscription.InboxID, request, 1, 2, 3)
	for i, entry := range f.read(t, subscription.InboxID, 0).Entries {
		payload, err := json.Marshal(events[i])
		if err != nil {
			t.Fatal(err)
		}
		sameJSON(t, entry.Payload, payload)
		requireEqual(t, entry.SubscriptionIDs, []uuid.UUID{subscription.ID})
	}
	requireEqual(t, f.actionsAfterSequence(t, subscription.ID), int64(3))
	requireBetween(t, "last_success_at", f.get(t, subscription.ID).LastSuccessAt, before, after)
	// The request is read again in five seconds, not at once.
	next := f.nextAttempt(t, subscription.ID)
	requireBetween(t, "next_attempt", &next, before.Add(actionsReadInterval), after.Add(actionsReadInterval))
	if due := f.source(t, claim); due != nil {
		t.Errorf("subscription %s is due again at once", due.ID)
	}
	f.makeDue(t, subscription.InboxID)
	f.exec(t, `UPDATE subscription SET next_attempt = now() WHERE id = $1`, subscription.ID)
	due = f.source(t, f.claim(t))
	if due == nil {
		t.Fatal("the subscription is not due once its wait is over")
	}
	requireEqual(t, due.ActionsAfterSequence, ptr(int64(3)))
}

func TestRecordActionEventsRecordsEachEventOnce(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, body("first"))
	request := subscription.Source.(ActionsSource).RequestID
	claim := f.claim(t)

	f.mustRecord(t, claim, subscription, eventsThrough(1, 3))
	// A read that began before the first was recorded still carries what it recorded, and the sequence in the
	// row, not the one it read, decides what is new.
	f.mustRecord(t, claim, subscription, eventsThrough(1, 3))
	f.mustRecord(t, claim, subscription, eventsThrough(2, 5))
	f.mustRecord(t, claim, subscription, nil)

	f.requireEntries(t, subscription.InboxID, request, 1, 2, 3, 4, 5)
	requireEqual(t, f.actionsAfterSequence(t, subscription.ID), int64(5))
}

func TestRecordActionEventsRefusesAGapAndRecordsNothingOfTheRead(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, body("first"))
	request := subscription.Source.(ActionsSource).RequestID
	claim := f.claim(t)
	f.mustRecord(t, claim, subscription, eventsThrough(1, 2))
	if err := f.SourceFailed(t.Context(), claim, f.get(t, subscription.ID), FailureUnavailable, "HTTP 503", time.Minute); err != nil {
		t.Fatal(err)
	}
	failure := f.get(t, subscription.ID).Failure

	for name, events := range map[string][]ActionEvent{
		"a read that starts past the next sequence": eventsThrough(4, 5),
		"a read that skips one in the middle":       append(eventsThrough(3, 3), eventsThrough(5, 5)...),
	} {
		t.Run(name, func(t *testing.T) {
			err := f.record(claim, subscription, events)
			if !errors.Is(err, ErrActionHistoryGap) {
				t.Fatalf("error = %v, want ErrActionHistoryGap", err)
			}
			f.requireEntries(t, subscription.InboxID, request, 1, 2)
			requireEqual(t, f.actionsAfterSequence(t, subscription.ID), int64(2))
			// The read did not succeed, so the failure it follows stays.
			requireEqual(t, f.get(t, subscription.ID).Failure, failure)
		})
	}
}

func TestRecordActionEventsLeavesASubscriptionThatChangedSinceItWasRead(t *testing.T) {
	for name, change := range map[string]func(f fixture, t *testing.T, subscription Subscription){
		"cancelled": func(f fixture, t *testing.T, subscription Subscription) { f.cancel(t, subscription.ID) },
		"expired": func(f fixture, t *testing.T, subscription Subscription) {
			f.exec(t, `UPDATE subscription SET expires_at = now() - interval '1 second' WHERE id = $1`, subscription.ID)
		},
		"renewed": func(f fixture, t *testing.T, subscription Subscription) {
			f.renew(t, subscription.ID, subscription.Version, 14)
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newClaimFixture(t)
			subscription := f.subscribe(t, body("first"))
			claim := f.claim(t)
			change(f, t, subscription)
			f.exec(t, `UPDATE subscription SET next_attempt = now() - interval '1 minute' WHERE id = $1`, subscription.ID)
			stored := f.nextAttempt(t, subscription.ID)

			f.mustRecord(t, claim, subscription, eventsThrough(1, 3))

			f.requireEntries(t, subscription.InboxID, subscription.Source.(ActionsSource).RequestID)
			requireEqual(t, f.actionsAfterSequence(t, subscription.ID), int64(0))
			requireEqual(t, f.get(t, subscription.ID).LastSuccessAt, (*time.Time)(nil))
			requireEqual(t, f.nextAttempt(t, subscription.ID), stored)
		})
	}
}

func TestRecordActionEventsIsFencedOnTheClaim(t *testing.T) {
	for name, lose := range map[string]func(f fixture, t *testing.T, subscription Subscription){
		"taken over": func(f fixture, t *testing.T, subscription Subscription) {
			f.exec(t, `UPDATE inbox SET claim = $2 WHERE id = $1`, subscription.InboxID, uuid.New())
		},
		"lease ended": func(f fixture, t *testing.T, subscription Subscription) {
			f.exec(t, `UPDATE inbox SET claim_until = now() - interval '1 second' WHERE id = $1`, subscription.InboxID)
		},
		"inbox retired": func(f fixture, t *testing.T, subscription Subscription) {
			if err := f.Retire(t.Context(), f.incarnation(), subscription.InboxID); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newClaimFixture(t)
			subscription := f.subscribe(t, body("first"))
			claim := f.claim(t)
			lose(f, t, subscription)

			if err := f.record(claim, subscription, eventsThrough(1, 2)); !errors.Is(err, ErrClaimLost) {
				t.Fatalf("error = %v, want ErrClaimLost", err)
			}
			f.requireEntries(t, subscription.InboxID, subscription.Source.(ActionsSource).RequestID)
			requireEqual(t, f.actionsAfterSequence(t, subscription.ID), int64(0))
		})
	}
}

func TestRecordActionEventsNeedsNoCapacityToReplayAndStopsAtTheEntryQuota(t *testing.T) {
	quotas := defaultQuotas()
	quotas.EntriesPerInbox = 2
	f := newFixture(t, quotas)
	f.exec(t, `TRUNCATE inbox CASCADE`)
	subscription := f.subscribe(t, body("first"))
	request := subscription.Source.(ActionsSource).RequestID
	claim := f.claim(t)

	f.mustRecord(t, claim, subscription, eventsThrough(1, 2))
	f.mustRecord(t, claim, subscription, eventsThrough(1, 2))
	requireQuota(t, f.record(claim, subscription, eventsThrough(1, 3)), EntriesPerInbox, 2)

	f.requireEntries(t, subscription.InboxID, request, 1, 2)
	requireEqual(t, f.actionsAfterSequence(t, subscription.ID), int64(2))
}

func TestRecordActionEventsClearsTheFailureOfItsSubscriptionAlone(t *testing.T) {
	f := newClaimFixture(t)
	first := f.subscribe(t, body("first"))
	second := f.subscribe(t, body("second"))
	claim := f.claim(t)
	for _, subscription := range []Subscription{first, second} {
		if err := f.SourceFailed(t.Context(), claim, subscription, FailureRateLimited, "HTTP 429", time.Minute); err != nil {
			t.Fatal(err)
		}
	}
	untouched := f.get(t, second.ID)

	before := time.Now()
	f.mustRecord(t, claim, first, nil)
	after := time.Now()

	recovered := f.get(t, first.ID)
	requireEqual(t, recovered.Failure, (*SourceFailure)(nil))
	requireEqual(t, recovered.RetryAt, (*time.Time)(nil))
	requireBetween(t, "last_success_at", recovered.LastSuccessAt, before, after)
	requireEqual(t, f.get(t, second.ID), untouched)
	requireEqual(t, f.read(t, first.InboxID, 0).Inbox.LastCursor, int64(0))
}

func TestOverlappingSubscriptionsOfARequestShareItsEntries(t *testing.T) {
	f := newClaimFixture(t)
	first := f.subscribe(t, body("first"))
	overlapping := body("overlapping")
	overlapping.Source = first.Source
	second := f.subscribe(t, overlapping)
	claim := f.claim(t)

	f.mustRecord(t, claim, first, eventsThrough(1, 3))
	f.mustRecord(t, claim, second, eventsThrough(1, 3))

	f.requireEntries(t, first.InboxID, first.Source.(ActionsSource).RequestID, 1, 2, 3)
	page := f.read(t, first.InboxID, 0)
	for _, entry := range page.Entries {
		requireEqual(t, len(entry.SubscriptionIDs), 2)
	}
	requireEqual(t, page.Inbox.Acknowledged, int64(0))
	requireEqual(t, f.actionsAfterSequence(t, second.ID), int64(3))
}

func TestConcurrentSourcesOfAnInboxCommitOnePrefix(t *testing.T) {
	f := newClaimFixture(t)
	first := f.subscribe(t, body("first"))
	second := f.subscribe(t, body("second"))
	claim := f.claim(t)

	var wg sync.WaitGroup
	for _, subscription := range []Subscription{first, second} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := f.record(claim, subscription, eventsThrough(1, 1)); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()

	page := f.read(t, first.InboxID, 0)
	requireEqual(t, len(page.Entries), 2)
	requests := map[uuid.UUID]bool{}
	for i, entry := range page.Entries {
		requireEqual(t, entry.Cursor, int64(i+1))
		requests[entry.Event.(ActionsEvent).RequestID] = true
	}
	requireEqual(t, requests, map[uuid.UUID]bool{
		first.Source.(ActionsSource).RequestID: true, second.Source.(ActionsSource).RequestID: true,
	})
}
