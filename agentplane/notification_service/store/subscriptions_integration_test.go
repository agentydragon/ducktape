package store

import (
	"errors"
	"fmt"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func requireQuota(t *testing.T, err error, quota Quota, limit int) {
	t.Helper()
	var quotaErr *QuotaError
	if !errors.As(err, &quotaErr) || quotaErr.Quota != quota || quotaErr.Limit != limit {
		t.Fatalf("error = %v, want a quota error for %d, limit %d", err, quota, limit)
	}
}

func requireEqual(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %+v\nwant %+v", got, want)
	}
}

func TestOnlyActiveSubscriptionsConsumeInboxQuota(t *testing.T) {
	for _, inactive := range []string{"cancelled", "expired"} {
		for _, limit := range []int{2, 64} {
			t.Run(fmt.Sprintf("%s/%d", inactive, limit), func(t *testing.T) {
				quotas := defaultQuotas()
				quotas.ActiveSubscriptionsPerInbox = limit
				f := newFixture(t, quotas)
				base := body("slot-0")
				keyed := func(key string) Subscribe {
					creation := base
					creation.IdempotencyKey = key
					return creation
				}
				var subscriptions []Subscription
				for i := range limit {
					subscriptions = append(subscriptions, f.subscribe(t, keyed(fmt.Sprintf("slot-%d", i))))
				}
				extra := keyed("extra")
				_, err := f.Subscribe(t.Context(), f.owner, extra, nil)
				requireQuota(t, err, ActiveSubscriptionsPerInbox, limit)
				// An idempotent replay and the renewal of an active subscription need no other slot.
				requireEqual(t, f.subscribe(t, keyed("slot-0")), subscriptions[0])
				renewed := f.renew(t, subscriptions[0].ID, 1, 7)
				if renewed.Version != 2 {
					t.Fatalf("version = %d, want 2", renewed.Version)
				}
				// The quota is per inbox, not shared across an account's Sessions.
				otherSession := extra
				otherSession.SessionID = "other-session"
				if other := f.subscribe(t, otherSession); other.InboxID == subscriptions[0].InboxID {
					t.Fatal("another Session shares the inbox")
				}

				if inactive == "cancelled" {
					f.cancel(t, renewed.ID)
				} else {
					f.exec(t, `UPDATE subscription SET expires_at = now() - interval '1 second' WHERE id = $1`, renewed.ID)
				}
				replacement := f.subscribe(t, extra)
				if replacement.InboxID != renewed.InboxID {
					t.Fatal("the replacement landed in another inbox")
				}
				// The inactive subscription keeps its idempotency key, and a replay does not revive it.
				retained := f.get(t, renewed.ID)
				requireEqual(t, f.subscribe(t, keyed("slot-0")), retained)
				differing := keyed("slot-0")
				differing.LifetimeDays = 30
				if _, err := f.Subscribe(t.Context(), f.owner, differing, nil); !errors.Is(err, ErrIdempotencyKeyReused) {
					t.Fatalf("error = %v, want ErrIdempotencyKeyReused", err)
				}
				_, err = f.Subscribe(t.Context(), f.owner, keyed("overflow"), nil)
				requireQuota(t, err, ActiveSubscriptionsPerInbox, limit)

				if inactive == "expired" {
					_, err := f.Renew(t.Context(), f.incarnation(), renewed.ID, retained.Version, 7)
					requireQuota(t, err, ActiveSubscriptionsPerInbox, limit)
					// A rejected revival leaves the old record and its version as they were.
					requireEqual(t, f.get(t, renewed.ID), retained)
					f.cancel(t, replacement.ID)
					revived := f.renew(t, renewed.ID, retained.Version, 7)
					if !revived.ExpiresAt.After(time.Now()) || revived.Version != retained.Version+1 {
						t.Fatalf("revived = %+v", revived)
					}
				}
				if got := f.count(t, `SELECT count(*) FROM subscription WHERE inbox_id = $1`, renewed.InboxID); got != limit+1 {
					t.Fatalf("%d subscriptions, want %d", got, limit+1)
				}
			})
		}
	}
}

func TestCreationAndExpiredRenewalShareTheLastActiveSlot(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	base := body("slot-0")
	var subscriptions []Subscription
	for i := range 64 {
		creation := base
		creation.IdempotencyKey = fmt.Sprintf("slot-%d", i)
		subscriptions = append(subscriptions, f.subscribe(t, creation))
	}
	expired := subscriptions[0]
	f.exec(t, `UPDATE subscription SET expires_at = now() - interval '1 second' WHERE id = $1`, expired.ID)

	last := base
	last.IdempotencyKey = "last-slot"
	results := make([]error, 2)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		_, results[0] = f.Subscribe(t.Context(), f.owner, last, nil)
	}()
	go func() {
		defer wg.Done()
		_, results[1] = f.Renew(t.Context(), f.incarnation(), expired.ID, 1, 7)
	}()
	wg.Wait()

	failures := 0
	for _, err := range results {
		if err != nil {
			requireQuota(t, err, ActiveSubscriptionsPerInbox, 64)
			failures++
		}
	}
	if failures != 1 {
		t.Fatalf("%d of the two calls failed, want exactly one: %v", failures, results)
	}
	if got := f.count(t, `SELECT count(*) FROM subscription WHERE inbox_id = $1 AND NOT cancelled AND expires_at > now()`, expired.InboxID); got != 64 {
		t.Fatalf("%d active subscriptions, want 64", got)
	}
}

func TestConfiguredInboxAndEntryQuotas(t *testing.T) {
	quotas := defaultQuotas()
	quotas.InboxesPerAccount = 1
	quotas.EntriesPerInbox = 2
	f := newFixture(t, quotas)
	first := body("first")
	subscription := f.subscribe(t, first)
	second := first
	second.SessionID = "second"
	_, err := f.Subscribe(t.Context(), f.owner, second, nil)
	requireQuota(t, err, InboxesPerAccount, 1)
	// Another account has its own capacity under the same configuration.
	if _, err := f.Subscribe(t.Context(), newOwner(), first, nil); err != nil {
		t.Fatal(err)
	}

	request := first.Source.(ActionsSource).RequestID
	event := func(sequence int64) EventIdentity { return ActionsEvent{RequestID: request, Sequence: sequence} }
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event(1), entryPayloadForTest(t, event(1)))
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event(2), entryPayloadForTest(t, event(2)))
	// A replay of events the inbox holds needs no capacity.
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event(2), `{}`)
	requireQuota(t, f.appendEvent(subscription.InboxID, subscription.ID, event(3), entryPayloadForTest(t, event(3))), EntriesPerInbox, 2)
	page := f.read(t, subscription.InboxID, 0)
	if page.Inbox.LastCursor != 2 || len(page.Entries) != 2 {
		t.Fatalf("last cursor %d with %d entries, want 2 and 2", page.Inbox.LastCursor, len(page.Entries))
	}
}

func TestOwnerIdempotenceCancelAndVersion(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	first := body("first")
	results := make([]Subscription, 2)
	var wg sync.WaitGroup
	for i := range results {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var err error
			results[i], err = f.Subscribe(t.Context(), f.owner, first, nil)
			if err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	requireEqual(t, results[0], results[1])
	subscription := results[0]
	if subscription.IdempotencyKey != "first" || subscription.Version != 1 || subscription.Cancelled {
		t.Fatalf("subscription = %+v", subscription)
	}
	requireEqual(t, subscription.Source, first.Source)
	if got := f.count(t, `SELECT count(*) FROM subscription WHERE inbox_id = $1`, subscription.InboxID); got != 1 {
		t.Fatalf("%d subscriptions, want 1", got)
	}

	// The key is local to the Session's inbox, not the whole Sandbox or account.
	otherSession := first
	otherSession.SessionID = "other-session"
	other := f.subscribe(t, otherSession)
	if other.InboxID == subscription.InboxID || other.ID == subscription.ID || other.IdempotencyKey != subscription.IdempotencyKey {
		t.Fatalf("other = %+v", other)
	}
	otherRequest := first
	otherRequest.Source = ActionsSource{RequestID: uuid.New()}
	afterSequence := first
	afterSequence.Source = ActionsSource{RequestID: first.Source.(ActionsSource).RequestID, AfterSequence: 1}
	for _, creation := range []Subscribe{otherRequest, afterSequence} {
		if _, err := f.Subscribe(t.Context(), f.owner, creation, nil); !errors.Is(err, ErrIdempotencyKeyReused) {
			t.Fatalf("error = %v, want ErrIdempotencyKeyReused", err)
		}
	}
	if _, err := f.Read(t.Context(), Owner{Account: newOwner(), SandboxUID: testSandboxUID}, subscription.InboxID, 0, 128); !errors.Is(err, ErrNotFound) {
		t.Fatalf("another account read the inbox: %v", err)
	}
	if _, err := f.GetSubscription(t.Context(), Owner{Account: newOwner(), SandboxUID: testSandboxUID}, subscription.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("another account read the subscription: %v", err)
	}
	if _, err := f.GetSubscription(t.Context(), f.incarnation(), uuid.New()); !errors.Is(err, ErrNotFound) {
		t.Fatalf("an unknown subscription was found: %v", err)
	}

	before := time.Now()
	renewed := f.renew(t, subscription.ID, 1, 30)
	if renewed.Version != 2 || renewed.ExpiresAt.Before(before.Add(30*24*time.Hour)) || renewed.ExpiresAt.After(time.Now().Add(30*24*time.Hour)) {
		t.Fatalf("renewed = %+v", renewed)
	}
	if _, err := f.Renew(t.Context(), f.incarnation(), subscription.ID, 1, 7); !errors.Is(err, ErrVersionChanged) {
		t.Fatalf("error = %v, want ErrVersionChanged", err)
	}
	cancelled := f.cancel(t, subscription.ID)
	if !cancelled.Cancelled || cancelled.Version != 3 {
		t.Fatalf("cancelled = %+v", cancelled)
	}
	requireEqual(t, f.cancel(t, subscription.ID), cancelled)
	// The cancelled subscription answers its creation key; it is not replaced.
	requireEqual(t, f.subscribe(t, first), cancelled)
	if _, err := f.Renew(t.Context(), f.incarnation(), subscription.ID, cancelled.Version, 7); !errors.Is(err, ErrVersionChanged) {
		t.Fatalf("error = %v, want ErrVersionChanged", err)
	}
}

func TestSubscriptionsAreListedInPagesInIDOrder(t *testing.T) {
	quotas := defaultQuotas()
	quotas.ActiveSubscriptionsPerInbox = 200
	f := newFixture(t, quotas)
	base := body("0")
	for i := range 130 {
		creation := base
		creation.IdempotencyKey = fmt.Sprint(i)
		f.subscribe(t, creation)
	}
	// Another account's subscriptions are not listed.
	newFixture(t, quotas).subscribe(t, base)

	first, err := f.ListSubscriptions(t.Context(), f.incarnation(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != 128 {
		t.Fatalf("first page holds %d, want 128", len(first))
	}
	second, err := f.ListSubscriptions(t.Context(), f.incarnation(), &first[len(first)-1].ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(second) != 2 {
		t.Fatalf("second page holds %d, want 2", len(second))
	}
	all := append(first, second...)
	for i := 1; i < len(all); i++ {
		if all[i-1].ID.String() >= all[i].ID.String() {
			t.Fatalf("subscriptions %d and %d are out of order", i-1, i)
		}
	}
}

func TestACreationBeyondItsBoundsIsRefused(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := body("")
	if _, err := f.Subscribe(t.Context(), f.owner, creation, nil); !errors.Is(err, ErrInvalid) {
		t.Fatalf("error = %v, want ErrInvalid", err)
	}
	creation = body("lifetime")
	creation.LifetimeDays = 31
	if _, err := f.Subscribe(t.Context(), f.owner, creation, nil); !errors.Is(err, ErrInvalid) {
		t.Fatalf("error = %v, want ErrInvalid", err)
	}
	subscription := f.subscribe(t, body("renewed"))
	for _, lifetimeDays := range []int64{0, 31} {
		if _, err := f.Renew(t.Context(), f.incarnation(), subscription.ID, 1, lifetimeDays); !errors.Is(err, ErrInvalid) {
			t.Fatalf("lifetime %d: error = %v, want ErrInvalid", lifetimeDays, err)
		}
	}
	if _, err := f.Renew(t.Context(), f.incarnation(), subscription.ID, 0, 7); !errors.Is(err, ErrInvalid) {
		t.Fatalf("error = %v, want ErrInvalid", err)
	}
}

func TestACurrentSourceFailureShowsWhenItWillBeRetried(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("failing"))
	if subscription.Failure != nil || subscription.RetryAt != nil || subscription.LastSuccessAt != nil {
		t.Fatalf("a new subscription shows %+v", subscription)
	}
	since := time.Now().UTC().Add(-time.Hour).Truncate(time.Microsecond)
	observed := since.Add(30 * time.Minute)
	retry := time.Now().UTC().Add(time.Minute).Truncate(time.Microsecond)
	succeeded := since.Add(-time.Hour)
	f.exec(t, `
		UPDATE subscription SET error = 'rate limited', error_kind = 'rate_limited', error_since = $2,
			error_observed_at = $3, next_attempt = $4, last_success_at = $5 WHERE id = $1`,
		subscription.ID, since, observed, retry, succeeded)

	failing := f.get(t, subscription.ID)
	if failing.Failure == nil || failing.Failure.Kind != FailureRateLimited || failing.Failure.Message != "rate limited" ||
		!failing.Failure.Since.Equal(since) || !failing.Failure.ObservedAt.Equal(observed) {
		t.Fatalf("failure = %+v", failing.Failure)
	}
	if failing.RetryAt == nil || !failing.RetryAt.Equal(retry) {
		t.Fatalf("retry at %v, want %v", failing.RetryAt, retry)
	}
	if failing.LastSuccessAt == nil || !failing.LastSuccessAt.Equal(succeeded) {
		t.Fatalf("last success at %v, want %v", failing.LastSuccessAt, succeeded)
	}

	// A cancelled or an expired subscription is not retried.
	if cancelled := f.cancel(t, subscription.ID); cancelled.Failure == nil || cancelled.RetryAt != nil {
		t.Fatalf("cancelled = %+v", cancelled)
	}
	expiring := f.subscribe(t, body("expiring"))
	f.exec(t, `
		UPDATE subscription SET error = 'unavailable', error_kind = 'unavailable', error_since = $2,
			error_observed_at = $2, next_attempt = $3, expires_at = now() - interval '1 second' WHERE id = $1`,
		expiring.ID, since, retry)
	if expired := f.get(t, expiring.ID); expired.Failure == nil || expired.RetryAt != nil {
		t.Fatalf("expired = %+v", expired)
	}
}
