package store

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

// timeTolerance covers PostgreSQL's microsecond timestamps and the clocks of the test and the database, which
// are one host's.
const timeTolerance = 2 * time.Millisecond

func ptr[T any](v T) *T { return &v }

// newClaimFixture is a fixture over a database without the other tests' inboxes: Claim takes whichever inbox
// is due, not the owner's.
func newClaimFixture(t *testing.T) fixture {
	t.Helper()
	f := newFixture(t, defaultQuotas())
	f.exec(t, `TRUNCATE inbox CASCADE`)
	return f
}

// addEvents commits events first through last of the subscription's Action request.
func (f fixture) addEvents(t *testing.T, subscription Subscription, first, last int64) {
	t.Helper()
	request := subscription.Source.(model.ActionsSource).RequestID
	for sequence := first; sequence <= last; sequence++ {
		event := model.ActionsEvent{RequestID: request, Sequence: sequence}
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	}
}

func (f fixture) claim(t *testing.T) Claim {
	t.Helper()
	claim, err := f.Claim(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if claim == nil {
		t.Fatal("no inbox was due")
	}
	return *claim
}

func (f fixture) requireNoneDue(t *testing.T) {
	t.Helper()
	claim, err := f.Claim(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if claim != nil {
		t.Fatalf("inbox %s was due", claim.ID)
	}
}

func (f fixture) nextWorkAt(t *testing.T) *time.Time {
	t.Helper()
	next, err := f.NextWorkAt(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	return next
}

func (f fixture) source(t *testing.T, claim Claim) *DueSubscription {
	t.Helper()
	due, err := f.Source(t.Context(), claim)
	if err != nil {
		t.Fatal(err)
	}
	return due
}

func (f fixture) prepareNotice(t *testing.T, claim Claim) Notice {
	t.Helper()
	notice, err := f.PrepareNotice(t.Context(), claim)
	if err != nil {
		t.Fatal(err)
	}
	if notice == nil {
		t.Fatal("no notice was prepared")
	}
	return *notice
}

// makeDue makes the inbox claimable now, as the passing of its deadline does.
func (f fixture) makeDue(t *testing.T, inboxID uuid.UUID) {
	t.Helper()
	f.exec(t, `UPDATE inbox SET next_attempt = now() - interval '1 second', claim_until = now() - interval '1 second' WHERE id = $1`, inboxID)
}

func requireBetween(t *testing.T, name string, got *time.Time, earliest, latest time.Time) {
	t.Helper()
	if got == nil {
		t.Fatalf("%s is nil, want between %s and %s", name, earliest.Format(time.RFC3339Nano), latest.Format(time.RFC3339Nano))
	}
	if got.Before(earliest.Add(-timeTolerance)) || got.After(latest.Add(timeTolerance)) {
		t.Fatalf("%s = %s, want between %s and %s", name, got.Format(time.RFC3339Nano),
			earliest.Format(time.RFC3339Nano), latest.Format(time.RFC3339Nano))
	}
}

func TestClaimTakesTheDueInboxThatHasWaitedLongest(t *testing.T) {
	f := newClaimFixture(t)
	newer := f.subscribe(t, sessionBody("newer", "newer"))
	older := f.subscribe(t, sessionBody("older", "older"))
	later := f.subscribe(t, sessionBody("later", "later"))
	retired := f.subscribe(t, sessionBody("retired", "retired"))
	if err := f.Retire(t.Context(), f.incarnation(), retired.InboxID); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE inbox SET next_attempt = now() - interval '10 seconds' WHERE id = $1`, newer.InboxID)
	f.exec(t, `UPDATE inbox SET next_attempt = now() - interval '20 seconds' WHERE id = $1`, older.InboxID)
	f.exec(t, `UPDATE inbox SET next_attempt = now() + interval '1 hour' WHERE id = $1`, later.InboxID)

	requireEqual(t, f.claim(t).ID, older.InboxID)
	requireEqual(t, f.claim(t).ID, newer.InboxID)
	f.requireNoneDue(t)
}

func TestClaimHoldsTheInboxForTheLease(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, f.body("first"))

	before := time.Now()
	claim := f.claim(t)
	after := time.Now()
	requireEqual(t, claim.ID, subscription.InboxID)
	requireEqual(t, claim.Holder(), f.incarnation())
	requireEqual(t, claim.DestinationRef, f.body("first").DestinationRef)
	requireEqual(t, claim.SessionID, f.sessionID)

	// Nothing else is due while the lease holds, and the next work is its expiry.
	f.requireNoneDue(t)
	requireBetween(t, "NextWorkAt", f.nextWorkAt(t), before.Add(LeaseDuration), after.Add(LeaseDuration))
}

func TestConcurrentClaimsNeverShareAnInbox(t *testing.T) {
	f := newClaimFixture(t)
	const inboxes, workers = 6, 12
	want := map[uuid.UUID]bool{}
	for i := range inboxes {
		want[f.subscribe(t, sessionBody("first", fmt.Sprintf("session-%d", i))).InboxID] = true
	}

	var (
		mutex   sync.Mutex
		claimed []uuid.UUID
		group   sync.WaitGroup
	)
	failures := make(chan error, workers)
	for range workers {
		group.Go(func() {
			claim, err := f.Claim(context.Background())
			if err != nil {
				failures <- err
				return
			}
			if claim != nil {
				mutex.Lock()
				claimed = append(claimed, claim.ID)
				mutex.Unlock()
			}
		})
	}
	group.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}

	got := map[uuid.UUID]bool{}
	for _, id := range claimed {
		if got[id] {
			t.Errorf("inbox %s was claimed twice", id)
		}
		got[id] = true
	}
	requireEqual(t, got, want)
}

func TestAnExpiredLeaseIsClaimedAgainAndTheOldClaimLosesItsWrites(t *testing.T) {
	f := newClaimFixture(t)
	f.subscribe(t, f.body("first"))
	old := f.claim(t)
	f.exec(t, `UPDATE inbox SET claim_until = now() - interval '1 second' WHERE id = $1`, old.ID)

	replacement := f.claim(t)
	requireEqual(t, replacement.ID, old.ID)
	if _, err := f.PendingEntryTimes(t.Context(), old); !errors.Is(err, ErrClaimLost) {
		t.Errorf("a write of the old claim = %v, want ErrClaimLost", err)
	}
	if _, err := f.PendingEntryTimes(t.Context(), replacement); err != nil {
		t.Errorf("a write of the new claim = %v", err)
	}
}

func TestEveryWriteOfAClaimIsFencedByTheLease(t *testing.T) {
	losses := map[string]func(t *testing.T, f fixture, claim Claim){
		"lease expired": func(t *testing.T, f fixture, claim Claim) {
			f.exec(t, `UPDATE inbox SET claim_until = now() - interval '1 second' WHERE id = $1`, claim.ID)
		},
		"claimed by another worker": func(t *testing.T, f fixture, claim Claim) {
			f.exec(t, `UPDATE inbox SET claim_until = now() - interval '1 second' WHERE id = $1`, claim.ID)
			f.claim(t)
		},
		"inbox retired": func(t *testing.T, f fixture, claim Claim) {
			if err := f.Retire(t.Context(), f.incarnation(), claim.ID); err != nil {
				t.Fatal(err)
			}
		},
	}
	for name, lose := range losses {
		t.Run(name, func(t *testing.T) {
			f := newClaimFixture(t)
			subscription := f.subscribe(t, f.body("first"))
			f.addEvents(t, subscription, 1, 1)
			claim := f.claim(t)
			notice := f.prepareNotice(t, claim)
			lose(t, f, claim)

			// ClearStale does nothing for a claim that saw no unavailable destination.
			pendingUnavailable := claim
			pendingUnavailable.staleCheckAt = ptr(time.Now())
			entry := &eventlogpb.EventEntry{Cursor: 1}
			writes := map[string]func() error{
				"Release":                 func() error { return f.Release(t.Context(), claim, nil, nil) },
				"ObserveStale":            func() error { return f.ObserveStale(t.Context(), claim, "not_found", time.Second) },
				"ClearStale":              func() error { return f.ClearStale(t.Context(), pendingUnavailable) },
				"PendingEntryTimes":       func() error { _, err := f.PendingEntryTimes(t.Context(), claim); return err },
				"CurrentNotice":           func() error { _, err := f.CurrentNotice(t.Context(), claim); return err },
				"PrepareNotice":           func() error { _, err := f.PrepareNotice(t.Context(), claim); return err },
				"CheckpointBeforeAttempt": func() error { return f.CheckpointBeforeAttempt(t.Context(), claim, notice, entry) },
				"Attempt":                 func() error { _, err := f.Attempt(t.Context(), claim, notice); return err },
				"DeliveryFailed":          func() error { return f.DeliveryFailed(t.Context(), claim, notice, "failed") },
				"Receipt":                 func() error { return f.Receipt(t.Context(), claim, notice, entry, 0) },
				"SourceFailed": func() error {
					return f.SourceFailed(t.Context(), claim, subscription, FailureUnavailable, "unavailable", time.Minute)
				},
				"SourceDeferred": func() error { return f.SourceDeferred(t.Context(), claim, subscription, time.Now().Add(time.Hour)) },
			}
			for write, run := range writes {
				if err := run(); !errors.Is(err, ErrClaimLost) {
					t.Errorf("%s = %v, want ErrClaimLost", write, err)
				}
			}
		})
	}
}

func TestInboxesWithNothingToWatchAreNotWorkedAgain(t *testing.T) {
	t.Run("retired", func(t *testing.T) {
		f := newClaimFixture(t)
		subscription := f.subscribe(t, f.body("first"))
		if err := f.Retire(t.Context(), f.incarnation(), subscription.InboxID); err != nil {
			t.Fatal(err)
		}
		f.requireNoneDue(t)
		if next := f.nextWorkAt(t); next != nil {
			t.Errorf("NextWorkAt = %s, want none", next)
		}
	})
	t.Run("last subscription cancelled", func(t *testing.T) {
		f := newClaimFixture(t)
		subscription := f.subscribe(t, f.body("first"))
		claim := f.claim(t)
		f.cancel(t, subscription.ID)
		if err := f.Release(t.Context(), claim, nil, nil); err != nil {
			t.Fatal(err)
		}
		f.requireNoneDue(t)
		if next := f.nextWorkAt(t); next != nil {
			t.Errorf("NextWorkAt = %s, want none", next)
		}
	})
}

func TestReleaseSchedulesTheNextWorkFromTheDurableState(t *testing.T) {
	failure := "ServiceError"
	// Offsets are from the start of the case, except wantAfterRelease, which is from the release.
	for _, c := range []struct {
		name string
		// entries are committed to the inbox.
		entries int64
		// notice is "awaiting" when one for the entries is prepared and awaits its receipts, "failed" when it ended
		// with an error.
		notice        string
		subscription  time.Duration
		deliveryError *string
		noticeDue     *time.Duration
		staleCheck    *time.Duration

		wantAtOffset     *time.Duration
		wantAfterRelease *time.Duration
	}{
		{name: "idle until the subscription is due", subscription: time.Hour, wantAtOffset: ptr(time.Hour)},
		{name: "a subscription that is overdue is due now", subscription: -time.Minute, wantAfterRelease: ptr(time.Duration(0))},
		{name: "entries without a deadline wait the backoff", entries: 1, subscription: time.Hour, wantAfterRelease: ptr(5 * time.Second)},
		{name: "entries wait for the debounce deadline", entries: 1, subscription: time.Hour, noticeDue: ptr(2 * time.Second), wantAtOffset: ptr(2 * time.Second)},
		{name: "a debounce deadline that has passed is due now", entries: 1, subscription: time.Hour, noticeDue: ptr(-time.Second), wantAfterRelease: ptr(time.Duration(0))},
		{name: "a subscription due before the debounce deadline", entries: 1, subscription: time.Second, noticeDue: ptr(2 * time.Second), wantAtOffset: ptr(time.Second)},
		{name: "a notice awaiting receipts is followed at the backoff", entries: 1, notice: "awaiting", subscription: time.Hour, wantAfterRelease: ptr(5 * time.Second)},
		{name: "a failed notice leaves nothing to follow", entries: 1, notice: "failed", subscription: time.Hour, wantAtOffset: ptr(time.Hour)},
		{name: "a failure postpones work that is due", entries: 1, subscription: time.Hour, deliveryError: &failure, noticeDue: ptr(-time.Second), wantAfterRelease: ptr(5 * time.Second)},
		{name: "a failure postpones a subscription that is overdue", subscription: -time.Minute, deliveryError: &failure, wantAfterRelease: ptr(5 * time.Second)},
		{name: "a failure does not bring later work forward", subscription: time.Hour, deliveryError: &failure, wantAtOffset: ptr(time.Hour)},
		{name: "an unavailable destination is rechecked at its deadline", entries: 1, subscription: time.Hour, noticeDue: ptr(-time.Second), staleCheck: ptr(10 * time.Second), wantAtOffset: ptr(10 * time.Second)},
		{name: "an unavailable destination past its deadline is due now", subscription: time.Hour, staleCheck: ptr(-10 * time.Second), wantAfterRelease: ptr(time.Duration(0))},
		{name: "an unavailable destination past its deadline waits out a failure", subscription: time.Hour, staleCheck: ptr(-10 * time.Second), deliveryError: &failure, wantAfterRelease: ptr(5 * time.Second)},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newClaimFixture(t)
			start := time.Now()
			subscription := f.subscribe(t, f.body("first"))
			f.addEvents(t, subscription, 1, c.entries)
			f.exec(t, `UPDATE subscription SET next_attempt = $2 WHERE id = $1`, subscription.ID, start.Add(c.subscription))
			if c.staleCheck != nil {
				f.exec(t, `UPDATE inbox SET stale_check_at = $2 WHERE id = $1`, subscription.InboxID, start.Add(*c.staleCheck))
			}
			claim := f.claim(t)
			switch c.notice {
			case "awaiting":
				f.prepareNotice(t, claim)
			case "failed":
				if err := f.DeliveryFailed(t.Context(), claim, f.prepareNotice(t, claim), "failed"); err != nil {
					t.Fatal(err)
				}
			}
			var noticeDueAt *time.Time
			if c.noticeDue != nil {
				noticeDueAt = ptr(start.Add(*c.noticeDue))
			}

			before := time.Now()
			if err := f.Release(t.Context(), claim, c.deliveryError, noticeDueAt); err != nil {
				t.Fatal(err)
			}
			after := time.Now()

			next := f.nextWorkAt(t)
			switch {
			case c.wantAtOffset != nil:
				requireBetween(t, "next work", next, start.Add(*c.wantAtOffset), start.Add(*c.wantAtOffset))
			case c.wantAfterRelease != nil:
				requireBetween(t, "next work", next, before.Add(*c.wantAfterRelease), after.Add(*c.wantAfterRelease))
			case next != nil:
				t.Errorf("next work = %s, want none", next)
			}
		})
	}
}

func TestReleaseShowsItsDeliveryErrorUntilTheNextRelease(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, f.body("first"))
	failure := "ServiceError"

	claim := f.claim(t)
	if err := f.Release(t.Context(), claim, &failure, nil); err != nil {
		t.Fatal(err)
	}
	requireEqual(t, f.read(t, subscription.InboxID, 0).Inbox.DeliveryError, &failure)

	f.makeDue(t, subscription.InboxID)
	claim = f.claim(t)
	requireEqual(t, f.read(t, subscription.InboxID, 0).Inbox.DeliveryError, &failure)
	if err := f.Release(t.Context(), claim, nil, nil); err != nil {
		t.Fatal(err)
	}
	requireEqual(t, f.read(t, subscription.InboxID, 0).Inbox.DeliveryError, (*string)(nil))
}

func TestSourceIsTheDueSubscriptionThatHasWaitedLongest(t *testing.T) {
	f := newClaimFixture(t)
	newer := f.subscribe(t, f.body("newer"))
	creation := f.body("older")
	creation.Source = model.ActionsSource{RequestID: uuid.New(), AfterSequence: 7}
	older := f.subscribe(t, creation)
	later := f.subscribe(t, f.body("later"))
	cancelled := f.subscribe(t, f.body("cancelled"))
	expired := f.subscribe(t, f.body("expired"))
	f.cancel(t, cancelled.ID)
	f.exec(t, `UPDATE subscription SET expires_at = now() - interval '1 second' WHERE id = $1`, expired.ID)
	for id, wait := range map[uuid.UUID]string{
		newer.ID: "10 seconds", older.ID: "20 seconds", cancelled.ID: "30 seconds", expired.ID: "40 seconds",
	} {
		f.exec(t, `UPDATE subscription SET next_attempt = now() - $2::interval WHERE id = $1`, id, wait)
	}
	f.exec(t, `UPDATE subscription SET next_attempt = now() + interval '1 hour' WHERE id = $1`, later.ID)
	claim := f.claim(t)

	due := f.source(t, claim)
	if due == nil {
		t.Fatal("no subscription was due")
	}
	requireEqual(t, due.ID, older.ID)
	requireEqual(t, due.ActionsAfterSequence, ptr(int64(7)))

	// Postponing it is not a failure, and it is not due again until then.
	if err := f.SourceDeferred(t.Context(), claim, due.Subscription, time.Now().Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	requireEqual(t, f.get(t, older.ID).Failure, (*SourceFailure)(nil))
	due = f.source(t, claim)
	if due == nil {
		t.Fatal("no subscription was due after the first was postponed")
	}
	requireEqual(t, due.ID, newer.ID)
	requireEqual(t, due.ActionsAfterSequence, ptr(int64(0)))

	if err := f.SourceDeferred(t.Context(), claim, due.Subscription, time.Now().Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if due := f.source(t, claim); due != nil {
		t.Errorf("subscription %s was due", due.ID)
	}
}

func TestSourceFailureIsShownOnTheSubscriptionAndRetried(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, f.body("first"))
	f.addEvents(t, subscription, 1, 2)
	claim := f.claim(t)

	fail := func(kind SourceFailureKind, message string) Subscription {
		t.Helper()
		due := f.source(t, claim)
		if due == nil {
			t.Fatal("the subscription was not due")
		}
		if err := f.SourceFailed(t.Context(), claim, due.Subscription, kind, message, 90*time.Second); err != nil {
			t.Fatal(err)
		}
		return f.get(t, subscription.ID)
	}
	retry := func() {
		t.Helper()
		f.exec(t, `UPDATE subscription SET next_attempt = now() - interval '1 second' WHERE id = $1`, subscription.ID)
	}

	before := time.Now()
	first := fail(FailureUnavailable, "unavailable")
	after := time.Now()
	if first.Failure == nil || first.Failure.Kind != FailureUnavailable || first.Failure.Message != "unavailable" {
		t.Fatalf("failure = %+v", first.Failure)
	}
	requireBetween(t, "Since", &first.Failure.Since, before, after)
	requireBetween(t, "ObservedAt", &first.Failure.ObservedAt, before, after)
	requireBetween(t, "RetryAt", first.RetryAt, before.Add(90*time.Second), after.Add(90*time.Second))
	if first.Version != subscription.Version {
		t.Errorf("a failure changed the version from %d to %d", subscription.Version, first.Version)
	}
	// The failure does not touch what the inbox holds, and the source is not due before the retry.
	requireEqual(t, len(f.read(t, subscription.InboxID, 0).Entries), 2)
	if due := f.source(t, claim); due != nil {
		t.Fatalf("subscription %s was due before its retry", due.ID)
	}

	// The same kind of failure keeps its start; a different kind starts again.
	retry()
	again := fail(FailureUnavailable, "still unavailable")
	if again.Failure.Message != "still unavailable" || !again.Failure.Since.Equal(first.Failure.Since) ||
		!again.Failure.ObservedAt.After(first.Failure.ObservedAt) {
		t.Errorf("same kind: %+v after %+v", again.Failure, first.Failure)
	}
	retry()
	changed := fail(FailureRateLimited, "rate limited")
	if changed.Failure.Kind != FailureRateLimited || !changed.Failure.Since.After(first.Failure.Since) {
		t.Errorf("new kind: %+v after %+v", changed.Failure, first.Failure)
	}
}

func TestSourceFailureHealthIsPerSubscription(t *testing.T) {
	f := newClaimFixture(t)
	failing := f.subscribe(t, f.body("failing"))
	healthy := f.subscribe(t, f.body("healthy"))
	f.exec(t, `UPDATE subscription SET next_attempt = now() - interval '1 minute' WHERE id = $1`, failing.ID)
	claim := f.claim(t)

	due := f.source(t, claim)
	if due == nil || due.ID != failing.ID {
		t.Fatalf("source = %+v, want the failing subscription", due)
	}
	if err := f.SourceFailed(t.Context(), claim, due.Subscription, FailureProcessingError, "failed", time.Minute); err != nil {
		t.Fatal(err)
	}
	requireEqual(t, f.get(t, healthy.ID).Failure, (*SourceFailure)(nil))
	if f.get(t, failing.ID).Failure == nil {
		t.Error("the failing subscription shows no failure")
	}
}

func TestAnOutcomeReportedAfterTheSubscriptionChangedIsIgnored(t *testing.T) {
	for _, c := range []struct {
		name   string
		change func(t *testing.T, f fixture, subscription Subscription)
	}{
		{"cancelled", func(t *testing.T, f fixture, subscription Subscription) { f.cancel(t, subscription.ID) }},
		{"renewed", func(t *testing.T, f fixture, subscription Subscription) {
			f.renew(t, subscription.ID, subscription.Version, 7)
		}},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newClaimFixture(t)
			created := f.subscribe(t, f.body("first"))
			claim := f.claim(t)
			due := f.source(t, claim)
			if due == nil {
				t.Fatal("the subscription was not due")
			}
			c.change(t, f, created)
			changed := f.get(t, created.ID)
			postponed := time.Now().Add(time.Hour).Truncate(time.Microsecond)

			if err := f.SourceFailed(t.Context(), claim, due.Subscription, FailureUnavailable, "unavailable", time.Minute); err != nil {
				t.Fatal(err)
			}
			if err := f.SourceDeferred(t.Context(), claim, due.Subscription, postponed); err != nil {
				t.Fatal(err)
			}
			requireEqual(t, f.get(t, created.ID), changed)
			if got := f.count(t, `SELECT count(*) FROM subscription WHERE id = $1 AND next_attempt = $2`, created.ID, postponed); got != 0 {
				t.Error("a postponement of the changed subscription took effect")
			}
		})
	}
}

func TestAFailureReportedAfterTheSubscriptionExpiredIsIgnored(t *testing.T) {
	f := newClaimFixture(t)
	created := f.subscribe(t, f.body("first"))
	claim := f.claim(t)
	due := f.source(t, claim)
	if due == nil {
		t.Fatal("the subscription was not due")
	}
	f.exec(t, `UPDATE subscription SET expires_at = now() - interval '1 second' WHERE id = $1`, created.ID)

	if err := f.SourceFailed(t.Context(), claim, due.Subscription, FailureUnavailable, "unavailable", time.Minute); err != nil {
		t.Fatal(err)
	}
	requireEqual(t, f.get(t, created.ID).Failure, (*SourceFailure)(nil))
}

func TestUnavailableDestinationKeepsInboxAndSubscriptionsAndSchedulesSourceWork(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, f.body("first"))
	f.exec(t, `UPDATE subscription SET next_attempt = now() + interval '1 hour' WHERE id = $1`, subscription.ID)
	claim := f.claim(t)

	before := time.Now()
	if err := f.ObserveStale(t.Context(), claim, "not_found", 30*time.Second); err != nil {
		t.Fatal(err)
	}
	pending := f.read(t, subscription.InboxID, 0).Inbox
	requireEqual(t, pending.DeliveryError, ptr("destination unavailable: not_found"))
	requireEqual(t, pending.Retired, false)
	requireBetween(t, "recheck", f.nextWorkAt(t), before.Add(30*time.Second), time.Now().Add(30*time.Second))
	requireEqual(t, f.get(t, subscription.ID).Cancelled, false)
	f.requireNoneDue(t)

	// A source due before the next destination recheck remains claimable.
	f.exec(t, `UPDATE subscription SET next_attempt = now() - interval '1 second' WHERE id = $1`, subscription.ID)
	f.exec(t, `UPDATE inbox SET next_attempt = now() - interval '1 second' WHERE id = $1`, subscription.InboxID)
	claim = f.claim(t)
	if err := f.ObserveStale(t.Context(), claim, "uid_changed", time.Hour); err != nil {
		t.Fatal(err)
	}
	if next := f.nextWorkAt(t); next == nil || next.After(time.Now()) {
		t.Errorf("NextWorkAt = %v, want the source due now", next)
	}
	requireEqual(t, f.read(t, subscription.InboxID, 0).Inbox.Retired, false)
	requireEqual(t, f.get(t, subscription.ID).Cancelled, false)
}

func TestAFoundDestinationClearsTheUnavailableState(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, f.body("first"))
	claim := f.claim(t)
	if err := f.ObserveStale(t.Context(), claim, "not_found", time.Hour); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE inbox SET stale_check_at = now() - interval '1 second', next_attempt = now() - interval '1 second' WHERE id = $1`,
		subscription.InboxID)
	claim = f.claim(t)
	if claim.staleCheckAt == nil {
		t.Fatal("the claim saw no unavailable state")
	}

	if err := f.ClearStale(t.Context(), claim); err != nil {
		t.Fatal(err)
	}
	if got := f.count(t, `SELECT count(*) FROM inbox WHERE id = $1 AND stale_check_at IS NULL`, subscription.InboxID); got != 1 {
		t.Fatal("the unavailable state was kept")
	}
	if err := f.ObserveStale(t.Context(), claim, "not_found", time.Hour); err != nil {
		t.Fatal(err)
	}
}

func TestPendingEntryTimesBoundTheEntriesNoNoticeCoversAndNoAgentAcknowledged(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, f.body("first"))
	claim := f.claim(t)
	times, err := f.PendingEntryTimes(t.Context(), claim)
	if err != nil || times != nil {
		t.Fatalf("PendingEntryTimes of an empty inbox = %+v, %v", times, err)
	}

	f.addEvents(t, subscription, 1, 4)
	base := time.Now().Add(-time.Hour).Truncate(time.Microsecond)
	for cursor := int64(1); cursor <= 4; cursor++ {
		f.exec(t, `UPDATE entry SET created_at = $3 WHERE inbox_id = $1 AND cursor = $2`,
			subscription.InboxID, cursor, base.Add(time.Duration(cursor)*time.Minute))
	}
	pending := func(wantFirst, wantLast int64) {
		t.Helper()
		times, err := f.PendingEntryTimes(t.Context(), claim)
		if err != nil || times == nil {
			t.Fatalf("PendingEntryTimes = %+v, %v; want entries %d through %d", times, err, wantFirst, wantLast)
		}
		if !times.First.Equal(base.Add(time.Duration(wantFirst)*time.Minute)) || !times.Last.Equal(base.Add(time.Duration(wantLast)*time.Minute)) {
			t.Fatalf("PendingEntryTimes = %+v, want entries %d through %d", times, wantFirst, wantLast)
		}
	}
	pending(1, 4)
	f.acknowledge(t, subscription.InboxID, 1)
	pending(2, 4)
	f.exec(t, `UPDATE inbox SET covered = 2 WHERE id = $1`, subscription.InboxID)
	pending(3, 4)
	f.exec(t, `UPDATE inbox SET expired_through = 3 WHERE id = $1`, subscription.InboxID)
	pending(4, 4)
	f.exec(t, `UPDATE inbox SET covered = 4 WHERE id = $1`, subscription.InboxID)
	if times, err := f.PendingEntryTimes(t.Context(), claim); err != nil || times != nil {
		t.Fatalf("PendingEntryTimes of a covered inbox = %+v, %v", times, err)
	}
}
