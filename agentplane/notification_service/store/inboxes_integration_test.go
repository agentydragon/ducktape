package store

import (
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func actionsEvents(request uuid.UUID, count int) []model.ActionsEvent {
	events := make([]model.ActionsEvent, count)
	for i := range events {
		events[i] = model.ActionsEvent{RequestID: request, Sequence: int64(i + 1)}
	}
	return events
}

func payloadOf(event model.ActionsEvent) string {
	return fmt.Sprintf(`{"sequence": %d, "state": "decision_pending", "at": "2026-01-01T00:00:00Z", "actor": null}`, event.Sequence)
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
	events := actionsEvents(first.Source.(model.ActionsSource).RequestID, 3)
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
		if entry.Cursor != int64(i+1) || entry.Event != model.EventIdentity(events[i]) || !slices.Equal(entry.SubscriptionIDs, matched) {
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
	if _, err := f.Acknowledge(t.Context(), f.incarnation(), a.InboxID, 4); !errors.Is(err, ErrCursorOutOfRange) {
		t.Fatalf("error = %v, want ErrCursorOutOfRange", err)
	}
	if _, err := f.Acknowledge(t.Context(), Owner{Account: newOwner(), SandboxUID: testSandboxUID}, a.InboxID, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}

func TestReadReconstructsTypedPayloadsAndKeepsGitHubJSONNative(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("typed-content-reader"))
	requestID := uuid.New()
	events := []model.EventIdentity{
		model.ActionsEvent{RequestID: requestID, Sequence: 1},
		model.GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 2},
		model.GitHubMergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 3},
		model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender", Name: "sandbox"}},
		model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest},
	}
	wantPayloads := make([][]byte, len(events))
	for i, event := range events {
		payload := entryPayloadForTest(t, event)
		wantPayloads[i] = []byte(payload)
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payload)
		if _, nativeJSON := event.(model.GitHubEvent); !nativeJSON {
			f.exec(t, `UPDATE entry SET payload = NULL WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, i+1)
		}
	}

	page := f.read(t, subscription.InboxID, 0)
	if len(page.Entries) != len(events) {
		t.Fatalf("read returned %d entries, want %d", len(page.Entries), len(events))
	}
	for i := range events {
		sameJSON(t, page.Entries[i].Payload, wantPayloads[i])
	}
}

func TestReadRejectsMissingNativeContentInsteadOfFallingBackToJSON(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("missing-typed-content"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	f.exec(t, `UPDATE entry SET action_state = NULL, action_at = NULL, action_actor_kind = NULL,
		action_actor_namespace = NULL, action_actor_name = NULL, action_actor_issuer = NULL,
		action_actor_subject = NULL WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)
	_, err := f.Read(t.Context(), f.incarnation(), subscription.InboxID, 0, 128)
	if err == nil || !strings.Contains(err.Error(), "entry has no Action content") {
		t.Fatalf("read error = %v, want a missing native content error", err)
	}
}

func TestReadIsBoundedAndOwned(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("first"))
	for _, limit := range []int{0, 129} {
		if _, err := f.Read(t.Context(), f.incarnation(), subscription.InboxID, 0, limit); !errors.Is(err, model.ErrInvalid) {
			t.Fatalf("limit %d: error = %v, want model.ErrInvalid", limit, err)
		}
	}
	if _, err := f.Read(t.Context(), f.incarnation(), subscription.InboxID, -1, 128); !errors.Is(err, model.ErrInvalid) {
		t.Fatalf("error = %v, want model.ErrInvalid", err)
	}
	if _, err := f.Read(t.Context(), f.incarnation(), uuid.New(), 0, 128); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
	if _, err := f.Acknowledge(t.Context(), f.incarnation(), subscription.InboxID, -1); !errors.Is(err, model.ErrInvalid) {
		t.Fatalf("error = %v, want model.ErrInvalid", err)
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
		CommandID: command, ThroughCursor: 3, Text: "notice", Attempted: true, Admitted: true, Confirmed: false, Error: &unreachable,
		RunnerCursor: 7,
	})
}

func TestConcurrentSourcesAllocateOneCommittedPrefix(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	first := f.subscribe(t, body("first"))
	second := f.subscribe(t, body("another-action"))
	requests := []uuid.UUID{first.Source.(model.ActionsSource).RequestID, second.Source.(model.ActionsSource).RequestID}

	var wg sync.WaitGroup
	for i, subscription := range []Subscription{first, second} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			event := model.ActionsEvent{RequestID: requests[i], Sequence: 1}
			if err := f.appendEvent(subscription.InboxID, subscription.ID, event, payloadOf(event)); err != nil {
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
		followed[entry.Event.(model.ActionsEvent).RequestID] = true
	}
	if !followed[requests[0]] || !followed[requests[1]] {
		t.Fatalf("entries followed %v, want %v", followed, requests)
	}
}

func TestRetirementStopsWorkWithoutErasingEntries(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := body("first")
	subscription := f.subscribe(t, creation)
	event := model.ActionsEvent{RequestID: creation.Source.(model.ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))

	if err := f.Retire(t.Context(), Owner{Account: newOwner(), SandboxUID: testSandboxUID}, subscription.InboxID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("another account retired the inbox: %v", err)
	}
	if err := f.Retire(t.Context(), f.incarnation(), subscription.InboxID); err != nil {
		t.Fatal(err)
	}
	// Retiring a retired inbox changes nothing.
	if err := f.Retire(t.Context(), f.incarnation(), subscription.InboxID); err != nil {
		t.Fatal(err)
	}
	if retired := f.get(t, subscription.ID); !retired.Cancelled || retired.Version != 2 {
		t.Fatalf("retired subscription = %+v", retired)
	}
	page := f.read(t, subscription.InboxID, 0)
	if !page.Inbox.Retired || page.Inbox.Acknowledged != 0 || len(page.Entries) != 1 {
		t.Fatalf("page = %+v", page)
	}
	inboxes, err := f.ListInboxes(t.Context(), f.incarnation())
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, inboxes, []Inbox{page.Inbox})

	later := creation
	later.IdempotencyKey = "later"
	if _, err := f.Subscribe(t.Context(), f.owner, later, nil); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("subscribe: error = %v, want ErrInboxRetired", err)
	}
	if _, err := f.Subscribe(t.Context(), f.owner, creation, nil); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("replayed subscribe: error = %v, want ErrInboxRetired", err)
	}
	if _, err := f.Renew(t.Context(), f.incarnation(), subscription.ID, 2, 7); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("renew: error = %v, want ErrInboxRetired", err)
	}
	if _, err := f.Cancel(t.Context(), f.incarnation(), subscription.ID); !errors.Is(err, ErrInboxRetired) {
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

	inboxes, err := f.ListInboxes(t.Context(), f.incarnation())
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
	events := actionsEvents(creation.Source.(model.ActionsSource).RequestID, 3)
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND action_at IS NOT NULL`, subscription.InboxID); got != 3 {
		t.Fatalf("%d entries have native Action content before expiry, want 3", got)
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
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND action_at IS NULL AND action_state IS NULL`, subscription.InboxID); got != 3 {
		t.Fatalf("%d entries without native Action content after expiry, want 3", got)
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
	events := actionsEvents(creation.Source.(model.ActionsSource).RequestID, 4)
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
	event := model.ActionsEvent{RequestID: creation.Source.(model.ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	recent := f.subscribe(t, func() model.Subscribe { other := creation; other.SessionID = "recently-retired"; return other }())

	for _, inboxID := range []uuid.UUID{subscription.InboxID, recent.InboxID} {
		if err := f.Retire(t.Context(), f.incarnation(), inboxID); err != nil {
			t.Fatal(err)
		}
	}
	f.exec(t, `UPDATE inbox SET updated_at = $2 WHERE id = $1`, subscription.InboxID, time.Now().Add(-31*24*time.Hour))
	f.cleanAll(t)

	inboxes, err := f.ListInboxes(t.Context(), f.incarnation())
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

func TestASandboxRecreatedUnderTheNameOwnsNoneOfItsPredecessorsInboxes(t *testing.T) {
	quotas := defaultQuotas()
	quotas.InboxesPerAccount = 1
	f := newFixture(t, quotas)
	predecessor := f.subscribe(t, body("predecessor"))
	successor := Owner{Account: f.owner, SandboxUID: "test-successor-uid"}

	if inboxes, err := f.ListInboxes(t.Context(), successor); err != nil || len(inboxes) != 0 {
		t.Fatalf("listed %+v, %v", inboxes, err)
	}
	if subscriptions, err := f.ListSubscriptions(t.Context(), successor, nil); err != nil || len(subscriptions) != 0 {
		t.Fatalf("listed %+v, %v", subscriptions, err)
	}
	for name, call := range map[string]func() error{
		"read":        func() error { _, err := f.Read(t.Context(), successor, predecessor.InboxID, 0, 128); return err },
		"acknowledge": func() error { _, err := f.Acknowledge(t.Context(), successor, predecessor.InboxID, 0); return err },
		"retire":      func() error { return f.Retire(t.Context(), successor, predecessor.InboxID) },
		"get":         func() error { _, err := f.GetSubscription(t.Context(), successor, predecessor.ID); return err },
		"renew":       func() error { _, err := f.Renew(t.Context(), successor, predecessor.ID, 1, 7); return err },
		"cancel":      func() error { _, err := f.Cancel(t.Context(), successor, predecessor.ID); return err },
	} {
		if err := call(); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: error = %v, want ErrNotFound", name, err)
		}
	}

	// The predecessor's inbox takes none of the successor's quota.
	creation := body("successor")
	creation.DestinationRef.UID = successor.SandboxUID
	subscription := f.subscribe(t, creation)
	if inboxes, err := f.ListInboxes(t.Context(), successor); err != nil || len(inboxes) != 1 || inboxes[0].ID != subscription.InboxID {
		t.Fatalf("listed %+v, %v; want only the successor's inbox", inboxes, err)
	}
}
