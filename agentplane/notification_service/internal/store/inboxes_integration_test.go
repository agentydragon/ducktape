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
	first := f.body("first")
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
	if _, err := f.Acknowledge(t.Context(), Owner{Identity: model.Identity{Name: "other-identity", UID: "other-identity-uid"}, SandboxUID: testSandboxUID}, a.InboxID, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v, want ErrNotFound", err)
	}
}

func TestReadReconstructsTypedPayloadsAndKeepsGitHubJSONNative(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, f.body("typed-content-reader"))
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
	subscription := f.subscribe(t, f.body("missing-typed-content"))
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
	subscription := f.subscribe(t, f.body("first"))
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

func TestOperatorReadSessionIsNamespaceScopedAndDoesNotAcknowledge(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	sub := f.subscribe(t, f.body("operator-retained-read"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, sub.InboxID, sub.ID, event, payloadOf(event))

	page, err := f.ReadSession(t.Context(), "test-namespace", f.sessionID, 0, 128)
	if err != nil || page.Inbox.ID != sub.InboxID || page.Inbox.Acknowledged != 0 || len(page.Entries) != 1 {
		t.Fatalf("operator page = %+v, %v", page, err)
	}
	if _, err := f.ReadSession(t.Context(), "other-namespace", f.sessionID, 0, 128); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross-namespace read = %v, want ErrNotFound", err)
	}
	if got := f.read(t, sub.InboxID, 0).Inbox.Acknowledged; got != 0 {
		t.Fatalf("read changed acknowledgement to %d", got)
	}
}

func TestReadReturnsTheInboxsLatestNotice(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, f.body("first"))
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
	first := f.subscribe(t, f.body("first"))
	second := f.subscribe(t, f.body("another-action"))
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
	creation := f.body("first")
	subscription := f.subscribe(t, creation)
	event := model.ActionsEvent{RequestID: creation.Source.(model.ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))

	if err := f.Retire(t.Context(), Owner{Identity: model.Identity{Name: "other-identity", UID: "other-identity-uid"}, SandboxUID: testSandboxUID}, subscription.InboxID); !errors.Is(err, ErrNotFound) {
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
	if _, err := f.Subscribe(t.Context(), f.incarnation(), later, nil); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("subscribe: error = %v, want ErrInboxRetired", err)
	}
	if _, err := f.Subscribe(t.Context(), f.incarnation(), creation, nil); !errors.Is(err, ErrInboxRetired) {
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
	first := f.body("first")
	second := first
	second.SessionID = "second-session"
	f.subscribe(t, first)
	f.subscribe(t, second)
	other.subscribe(t, other.body("first"))

	inboxes, err := f.ListInboxes(t.Context(), f.incarnation())
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes) != 2 || inboxes[0].ID.String() >= inboxes[1].ID.String() {
		t.Fatalf("inboxes = %+v, want two in ID order", inboxes)
	}
	sessions := []string{inboxes[0].SessionID, inboxes[1].SessionID}
	slices.Sort(sessions)
	wantSessions := []string{"second-session", f.sessionID}
	slices.Sort(wantSessions)
	requireEqual(t, sessions, wantSessions)
	requireEqual(t, inboxes[0].DestinationRef, first.DestinationRef)
}

func TestRetentionGapIsVisibleAndReplayKeepsTheTombstone(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := f.body("first")
	subscription := f.subscribe(t, creation)
	events := actionsEvents(creation.Source.(model.ActionsSource).RequestID, 3)
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND action_at IS NOT NULL`, subscription.InboxID); got != 3 {
		t.Fatalf("%d entries have native Action content before expiry, want 3", got)
	}
	f.acknowledge(t, subscription.InboxID, 3)
	f.exec(t, `UPDATE entry SET created_at = now() - interval '31 days' WHERE inbox_id = $1`, subscription.InboxID)
	f.cleanAll(t)

	page := f.read(t, subscription.InboxID, 0)
	if page.Inbox.ExpiredThrough != 3 || page.Inbox.Acknowledged != 3 || len(page.Entries) != 0 {
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
	creation := f.body("first")
	subscription := f.subscribe(t, creation)
	events := actionsEvents(creation.Source.(model.ActionsSource).RequestID, 4)
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	}
	f.acknowledge(t, subscription.InboxID, 4)
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

func TestUnreadProviderAndMessagePayloadsSurviveThirtyDayCleanup(t *testing.T) {
	f := newFixture(t, messageQuotas())
	creation := f.body("first")
	subscription := f.subscribe(t, creation)
	event := actionsEvents(creation.Source.(model.ActionsSource).RequestID, 1)[0]
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	f.send(t, f.message(newOwner(), "message", "Please keep this message."))
	f.exec(t, `UPDATE entry SET created_at = now() - interval '31 days' WHERE inbox_id = $1`, subscription.InboxID)

	f.cleanAll(t)
	page := f.read(t, subscription.InboxID, 0)
	if page.Inbox.ExpiredThrough != 0 || len(page.Entries) != 2 {
		t.Fatalf("unread entries after cleanup = %+v", page)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND (action_at IS NOT NULL OR message_body IS NOT NULL)`, subscription.InboxID); got != 2 {
		t.Fatalf("%d unread entries retained native content, want 2", got)
	}

	f.acknowledge(t, subscription.InboxID, 1)
	f.cleanAll(t)
	page = f.read(t, subscription.InboxID, 0)
	if page.Inbox.ExpiredThrough != 1 || len(page.Entries) != 1 || page.Entries[0].Cursor != 2 {
		t.Fatalf("message after acknowledging only the provider event = %+v", page)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 2 AND message_body = 'Please keep this message.'`, subscription.InboxID); got != 1 {
		t.Fatal("the unread authored message body was cleared")
	}
}

func TestInboxesRetiredLongAgoAreDeletedWithTheirRows(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := f.body("first")
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
	predecessor := f.subscribe(t, f.body("predecessor"))
	successor := Owner{Identity: f.identity, SandboxUID: "test-successor-uid"}

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
	creation := f.body("successor")
	creation.DestinationRef.UID = successor.SandboxUID
	creation.SessionID = uuid.NewString()
	subscription := f.subscribe(t, creation)
	if inboxes, err := f.ListInboxes(t.Context(), successor); err != nil || len(inboxes) != 1 || inboxes[0].ID != subscription.InboxID {
		t.Fatalf("listed %+v, %v; want only the successor's inbox", inboxes, err)
	}
}

func TestIdentityUIDFencesRetainedInboxAndSubscriptions(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, f.body("uid-owner"))
	owner := f.incarnation()
	owner.Identity.UID = "replacement-account-uid"
	inboxes, err := f.ListInboxes(t.Context(), owner)
	if err != nil || len(inboxes) != 0 {
		t.Fatalf("replacement inboxes = %v, %v", inboxes, err)
	}
	subscriptions, err := f.ListSubscriptions(t.Context(), owner, nil)
	if err != nil || len(subscriptions) != 0 {
		t.Fatalf("replacement subscriptions = %v, %v", subscriptions, err)
	}
	for name, access := range map[string]func() error{
		"read":         func() error { _, err := f.Read(t.Context(), owner, subscription.InboxID, 0, 10); return err },
		"acknowledge":  func() error { _, err := f.Acknowledge(t.Context(), owner, subscription.InboxID, 0); return err },
		"retire":       func() error { return f.Retire(t.Context(), owner, subscription.InboxID) },
		"subscription": func() error { _, err := f.GetSubscription(t.Context(), owner, subscription.ID); return err },
		"Session":      func() error { _, err := f.InboxIDForSession(t.Context(), owner, f.sessionID); return err },
	} {
		t.Run(name, func(t *testing.T) {
			if err := access(); !errors.Is(err, ErrNotFound) {
				t.Fatalf("replacement access = %v", err)
			}
		})
	}
	if _, err := f.Read(t.Context(), f.incarnation(), subscription.InboxID, 0, 10); err != nil {
		t.Fatal(err)
	}
	body := f.body("replacement-create")
	if _, err := f.Subscribe(t.Context(), owner, body, nil); !errors.Is(err, ErrNotFound) {
		t.Fatalf("replacement creation = %v", err)
	}
}

func TestLegacyInboxesStayRetainedWithoutIdentityAdoption(t *testing.T) {
	for _, legacyUID := range []string{"", "legacy-account-uid"} {
		t.Run(legacyUID, func(t *testing.T) {
			f := newIsolatedFixture(t, messageQuotas())
			body := f.body("retained")
			subscription := f.subscribe(t, body)
			message := f.message(newOwner(), "retained-message", "Retain this message.")
			sent := f.send(t, message)
			notice := f.prepareNotice(t, f.claim(t))
			f.exec(t, `UPDATE notice SET attempted = true WHERE command_id = $1`, notice.CommandID)
			var accountUID any
			if legacyUID != "" {
				accountUID = legacyUID
			}
			f.exec(t, `UPDATE inbox SET owner_uid = $2, owner_identity_name = NULL, owner_identity_uid = NULL WHERE id = $1`, subscription.InboxID, accountUID)
			retained := func() string {
				t.Helper()
				var row string
				if err := f.db.QueryRowContext(t.Context(), `SELECT jsonb_build_object(
					'inbox', to_jsonb(i),
					'subscriptions', (SELECT jsonb_agg(to_jsonb(s) ORDER BY id) FROM subscription s WHERE inbox_id = i.id),
					'entries', (SELECT jsonb_agg(to_jsonb(e) ORDER BY cursor) FROM entry e WHERE inbox_id = i.id),
					'messages', (SELECT jsonb_agg(to_jsonb(m) ORDER BY id) FROM message m WHERE inbox_id = i.id),
					'notices', (SELECT jsonb_agg(to_jsonb(n) ORDER BY command_id) FROM notice n WHERE inbox_id = i.id)
				)::text FROM inbox i WHERE id = $1`, subscription.InboxID).Scan(&row); err != nil {
					t.Fatal(err)
				}
				return row
			}
			before := retained()
			owner := f.incarnation()
			if inboxes, err := f.ListInboxes(t.Context(), owner); err != nil || len(inboxes) != 0 {
				t.Fatalf("Identity listed legacy inboxes: %+v, %v", inboxes, err)
			}
			for name, call := range map[string]func() error{
				"read":         func() error { _, err := f.Read(t.Context(), owner, subscription.InboxID, 0, 128); return err },
				"acknowledge":  func() error { _, err := f.Acknowledge(t.Context(), owner, subscription.InboxID, 1); return err },
				"retire":       func() error { return f.Retire(t.Context(), owner, subscription.InboxID) },
				"subscription": func() error { _, err := f.GetSubscription(t.Context(), owner, subscription.ID); return err },
				"renew":        func() error { _, err := f.Renew(t.Context(), owner, subscription.ID, 1, 7); return err },
				"cancel":       func() error { _, err := f.Cancel(t.Context(), owner, subscription.ID); return err },
				"open":         func() error { return f.OpenInbox(t.Context(), owner, body.DestinationRef, body.SessionID) },
				"subscribe":    func() error { _, err := f.Subscribe(t.Context(), owner, body, nil); return err },
			} {
				if err := call(); !errors.Is(err, ErrNotFound) {
					t.Errorf("%s = %v, want ErrNotFound", name, err)
				}
			}
			if _, err := f.Send(t.Context(), message); !errors.Is(err, ErrNoInbox) {
				t.Fatalf("legacy recipient message retry = %v", err)
			}
			if receipt, err := f.SentMessage(t.Context(), message.Sender, sent.ID); err != nil || receipt.ID != sent.ID {
				t.Fatalf("sender lost its retained receipt: %+v, %v", receipt, err)
			}
			if claim, err := f.Claim(t.Context()); err != nil || claim != nil {
				t.Fatalf("legacy inbox entered delivery queue: %+v, %v", claim, err)
			}
			if deadline, err := f.NextWorkAt(t.Context()); err != nil || deadline != nil {
				t.Fatalf("legacy inbox scheduled delivery: %v, %v", deadline, err)
			}
			if status, _, err := f.SessionStatus(t.Context(), body.DestinationRef.Namespace, body.SessionID, nil, Debounce{}, true, true); err != nil || status == nil || status.Inbox.ID != subscription.InboxID {
				t.Fatalf("operator lost retained inbox: %+v, %v", status, err)
			}
			if after := retained(); after != before {
				t.Fatal("legacy inbox, subscription, entries, or message changed")
			}
		})
	}
}

func TestAnIdentityOwnsExactlyItsIncarnationAndSandboxInboxes(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	owner := f.incarnation()
	sub := f.subscribe(t, f.body("identity-owner"))
	for _, candidate := range []Owner{
		{Identity: model.Identity{Name: owner.Identity.Name, UID: "replacement-identity-uid"}, SandboxUID: owner.SandboxUID},
		{Identity: owner.Identity, SandboxUID: "replacement-sandbox-uid"},
		{},
	} {
		if inboxes, err := f.ListInboxes(t.Context(), candidate); err != nil || len(inboxes) != 0 {
			t.Fatalf("another incarnation listed inboxes: %+v, %v", inboxes, err)
		}
		if _, err := f.Acknowledge(t.Context(), candidate, sub.InboxID, 0); !errors.Is(err, ErrNotFound) {
			t.Fatalf("another incarnation acknowledged inbox: %v", err)
		}
	}
	if _, err := f.Acknowledge(t.Context(), owner, sub.InboxID, 0); err != nil {
		t.Fatal(err)
	}
}
