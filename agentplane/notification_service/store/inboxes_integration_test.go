package store

import (
	"errors"
	"fmt"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func actionsEvents(request uuid.UUID, count int) []ActionsEvent {
	events := make([]ActionsEvent, count)
	for i := range events {
		events[i] = ActionsEvent{RequestID: request, Sequence: int64(i + 1)}
	}
	return events
}

func payloadOf(event ActionsEvent) string {
	return fmt.Sprintf(`{"sequence": %d, "state": "decision_pending"}`, event.Sequence)
}

func TestOverlappingSubscriptionsCommitOnePrefixAndReadDoesNotAcknowledge(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	first := body("first")
	overlap := first
	overlap.IdempotencyKey = "overlap"
	a, b := f.subscribe(t, first), f.subscribe(t, overlap)
	if a.InboxID != b.InboxID {
		t.Fatal("the subscriptions of one Session landed in two inboxes")
	}
	events := actionsEvents(first.Source.(ActionsSource).RequestID, 3)
	for _, event := range events {
		f.mustAppendEvent(t, a.InboxID, a.ID, event, payloadOf(event))
	}
	// The second subscription follows the same events: they are the same entries, matched twice.
	for _, event := range events {
		f.mustAppendEvent(t, a.InboxID, b.ID, event, payloadOf(event))
	}

	page := f.read(t, a.InboxID, 0)
	if len(page.Entries) != 3 {
		t.Fatalf("%d entries, want 3", len(page.Entries))
	}
	matched := []uuid.UUID{a.ID, b.ID}
	slices.SortFunc(matched, func(x, y uuid.UUID) int { return slices.Compare(x[:], y[:]) })
	for i, entry := range page.Entries {
		if entry.Cursor != int64(i+1) || entry.Event != EventIdentity(events[i]) || !slices.Equal(entry.SubscriptionIDs, matched) {
			t.Fatalf("entry %d = %+v", i, entry)
		}
		sameJSON(t, entry.Payload, []byte(payloadOf(events[i])))
	}
	if page.Inbox.LastCursor != 3 || page.Inbox.Acknowledged != 0 {
		t.Fatalf("inbox = %+v", page.Inbox)
	}
	if rest := f.read(t, a.InboxID, 1); len(rest.Entries) != 2 || rest.Entries[0].Cursor != 2 {
		t.Fatalf("entries after 1 = %+v", rest.Entries)
	}

	if got := f.acknowledge(t, a.InboxID, 2).Acknowledged; got != 2 {
		t.Fatalf("acknowledged = %d, want 2", got)
	}
	if got := f.acknowledge(t, a.InboxID, 1).Acknowledged; got != 2 {
		t.Fatalf("acknowledged = %d after a lower cursor, want 2", got)
	}
	if _, err := f.Acknowledge(t.Context(), f.owner, a.InboxID, 4); !errors.Is(err, ErrCursorOutOfRange) {
		t.Fatalf("error = %v, want ErrCursorOutOfRange", err)
	}
	if _, err := f.Acknowledge(t.Context(), newOwner(), a.InboxID, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}

func TestReadIsBoundedAndOwned(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("first"))
	for _, limit := range []int{0, 129} {
		if _, err := f.Read(t.Context(), f.owner, subscription.InboxID, 0, limit); !errors.Is(err, ErrInvalid) {
			t.Fatalf("limit %d: error = %v, want ErrInvalid", limit, err)
		}
	}
	if _, err := f.Read(t.Context(), f.owner, subscription.InboxID, -1, 128); !errors.Is(err, ErrInvalid) {
		t.Fatalf("error = %v, want ErrInvalid", err)
	}
	if _, err := f.Read(t.Context(), f.owner, uuid.New(), 0, 128); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
	if _, err := f.Acknowledge(t.Context(), f.owner, subscription.InboxID, -1); !errors.Is(err, ErrInvalid) {
		t.Fatalf("error = %v, want ErrInvalid", err)
	}
	page := f.read(t, subscription.InboxID, 0)
	if len(page.Entries) != 0 || page.Notice != nil || page.Inbox.ID != subscription.InboxID {
		t.Fatalf("page = %+v", page)
	}
}

func TestReadReturnsTheInboxsLatestNotice(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("first"))
	command := uuid.New()
	f.exec(t, `
		INSERT INTO notice (inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, error, runner_cursor)
		VALUES ($1, $2, 3, 'notice', true, true, false, 'unreachable', 7)`, subscription.InboxID, command)
	notice := f.read(t, subscription.InboxID, 0).Notice
	unreachable := "unreachable"
	requireEqual(t, notice, &Notice{
		CommandID: command, ThroughCursor: 3, Attempted: true, Admitted: true, Confirmed: false, Error: &unreachable,
	})
}

func TestConcurrentSourcesAllocateOneCommittedPrefix(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	first := f.subscribe(t, body("first"))
	second := f.subscribe(t, body("another-action"))
	requests := []uuid.UUID{first.Source.(ActionsSource).RequestID, second.Source.(ActionsSource).RequestID}

	var wg sync.WaitGroup
	for i, subscription := range []Subscription{first, second} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			event := ActionsEvent{RequestID: requests[i], Sequence: 1}
			if err := f.appendEvent(subscription.InboxID, subscription.ID, event, `{}`); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	page := f.read(t, first.InboxID, 0)
	if len(page.Entries) != 2 || page.Entries[0].Cursor != 1 || page.Entries[1].Cursor != 2 {
		t.Fatalf("entries = %+v, want cursors 1 and 2", page.Entries)
	}
	followed := map[uuid.UUID]bool{}
	for _, entry := range page.Entries {
		followed[entry.Event.(ActionsEvent).RequestID] = true
	}
	if !followed[requests[0]] || !followed[requests[1]] {
		t.Fatalf("entries followed %v, want %v", followed, requests)
	}
}

func TestRetirementStopsWorkWithoutErasingEntries(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := body("first")
	subscription := f.subscribe(t, creation)
	event := ActionsEvent{RequestID: creation.Source.(ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))

	if err := f.Retire(t.Context(), newOwner(), subscription.InboxID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("another account retired the inbox: %v", err)
	}
	if err := f.Retire(t.Context(), f.owner, subscription.InboxID); err != nil {
		t.Fatal(err)
	}
	// Retiring a retired inbox changes nothing.
	if err := f.Retire(t.Context(), f.owner, subscription.InboxID); err != nil {
		t.Fatal(err)
	}
	if retired := f.get(t, subscription.ID); !retired.Cancelled || retired.Version != 2 {
		t.Fatalf("retired subscription = %+v", retired)
	}
	page := f.read(t, subscription.InboxID, 0)
	if !page.Inbox.Retired || page.Inbox.Acknowledged != 0 || len(page.Entries) != 1 {
		t.Fatalf("page = %+v", page)
	}
	inboxes, err := f.ListInboxes(t.Context(), f.owner)
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, inboxes, []Inbox{page.Inbox})

	later := creation
	later.IdempotencyKey = "later"
	if _, err := f.Subscribe(t.Context(), f.owner, later); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("subscribe: error = %v, want ErrInboxRetired", err)
	}
	if _, err := f.Subscribe(t.Context(), f.owner, creation); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("replayed subscribe: error = %v, want ErrInboxRetired", err)
	}
	if _, err := f.Renew(t.Context(), f.owner, subscription.ID, 2, 7); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("renew: error = %v, want ErrInboxRetired", err)
	}
	if _, err := f.Cancel(t.Context(), f.owner, subscription.ID); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("cancel: error = %v, want ErrInboxRetired", err)
	}
}

func TestInboxesAreListedForTheirOwnerOnly(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	other := newFixture(t, defaultQuotas())
	first := body("first")
	second := first
	second.SessionID = "second-session"
	f.subscribe(t, first)
	f.subscribe(t, second)
	other.subscribe(t, first)

	inboxes, err := f.ListInboxes(t.Context(), f.owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes) != 2 || inboxes[0].ID.String() >= inboxes[1].ID.String() {
		t.Fatalf("inboxes = %+v, want two in ID order", inboxes)
	}
	sessions := []string{inboxes[0].SessionID, inboxes[1].SessionID}
	slices.Sort(sessions)
	requireEqual(t, sessions, []string{"second-session", "test-session"})
	requireEqual(t, inboxes[0].DestinationRef, first.DestinationRef)
}

func TestRetentionGapIsVisibleAndReplayKeepsTheTombstone(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := body("first")
	subscription := f.subscribe(t, creation)
	events := actionsEvents(creation.Source.(ActionsSource).RequestID, 3)
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	}
	f.exec(t, `UPDATE entry SET created_at = now() - interval '31 days' WHERE inbox_id = $1`, subscription.InboxID)
	f.cleanAll(t)

	page := f.read(t, subscription.InboxID, 0)
	if page.Inbox.ExpiredThrough != 3 || page.Inbox.Acknowledged != 0 || len(page.Entries) != 0 {
		t.Fatalf("page = %+v", page)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND payload IS NULL`, subscription.InboxID); got != 3 {
		t.Fatalf("%d entries without a payload, want 3", got)
	}
	// A late overlapping subscription replaying the expired events finds their identities and adds none.
	late := creation
	late.IdempotencyKey = "late-overlap"
	lateSubscription := f.subscribe(t, late)
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, lateSubscription.ID, event, payloadOf(event))
	}
	if got := f.read(t, subscription.InboxID, 0).Inbox.LastCursor; got != 3 {
		t.Fatalf("last cursor = %d, want 3", got)
	}
}

func TestPayloadsExpireOnlyUpToTheFirstEntryYoungerThanThirtyDays(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := body("first")
	subscription := f.subscribe(t, creation)
	events := actionsEvents(creation.Source.(ActionsSource).RequestID, 4)
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	}
	f.exec(t, `UPDATE entry SET created_at = now() - interval '31 days' WHERE inbox_id = $1 AND cursor IN (1, 2, 4)`, subscription.InboxID)
	f.cleanAll(t)

	page := f.read(t, subscription.InboxID, 0)
	if page.Inbox.ExpiredThrough != 2 {
		t.Fatalf("expired through %d, want 2", page.Inbox.ExpiredThrough)
	}
	// Reading skips the expired prefix and continues in order, past the entry that is old but not in it.
	if len(page.Entries) != 2 || page.Entries[0].Cursor != 3 || page.Entries[1].Cursor != 4 {
		t.Fatalf("entries = %+v, want cursors 3 and 4", page.Entries)
	}
}

func TestInboxesRetiredLongAgoAreDeletedWithTheirRows(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := body("first")
	subscription := f.subscribe(t, creation)
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, ActionsEvent{RequestID: creation.Source.(ActionsSource).RequestID, Sequence: 1}, `{}`)
	recent := f.subscribe(t, func() Subscribe { other := creation; other.SessionID = "recently-retired"; return other }())

	for _, inboxID := range []uuid.UUID{subscription.InboxID, recent.InboxID} {
		if err := f.Retire(t.Context(), f.owner, inboxID); err != nil {
			t.Fatal(err)
		}
	}
	f.exec(t, `UPDATE inbox SET updated_at = $2 WHERE id = $1`, subscription.InboxID, time.Now().Add(-31*24*time.Hour))
	f.cleanAll(t)

	inboxes, err := f.ListInboxes(t.Context(), f.owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes) != 1 || inboxes[0].ID != recent.InboxID {
		t.Fatalf("inboxes = %+v, want only the recently retired one", inboxes)
	}
	for _, table := range []string{"subscription", "entry"} {
		if got := f.count(t, `SELECT count(*) FROM `+table+` WHERE inbox_id = $1`, subscription.InboxID); got != 0 {
			t.Fatalf("%d %s rows of the deleted inbox remain", got, table)
		}
	}
}
