package store

import (
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestEntryLegacyCleanupRetainsNativeAndGitHubContent(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 20); err != nil {
		t.Fatal(err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("legacy-cleanup"))
	action := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	mergeability := model.GitHubMergeabilityEvent{AppID: 17, RepositoryID: 29, PullRequestNumber: 7, Sequence: 3}
	message := model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender", Name: "workload"}}
	github := model.GitHubEvent{AppID: 17, DeliveryID: uuid.New(), RepositoryID: 29, Event: model.EventPullRequest}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, action, entryPayloadForTest(t, action))
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, mergeability, entryPayloadForTest(t, mergeability))
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, message, `{"body":"private message","from_session":"session-a"}`)
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, github, `{"native":"github"}`)
	f.exec(t, `UPDATE entry SET payload = '{"historical":"owned"}'::jsonb
		WHERE inbox_id = $1 AND identity_provider IN ('actions', 'github_mergeability', 'message')`, subscription.InboxID)
	f.exec(t, `UPDATE inbox SET expired_through = 1 WHERE id = $1`, subscription.InboxID)
	if err := migrations.UpTo(ctx, db, 21); err != nil {
		t.Fatal(err)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_constraint WHERE conrelid = 'entry'::regclass
		AND conname = 'entry_owned_legacy_content_absent' AND NOT convalidated`); got != 1 {
		t.Fatalf("new legacy-absence constraint has %d unvalidated rows, want 1", got)
	}
	if err := CleanupEntryLegacyContent(ctx, db, 2); err != nil {
		t.Fatal(err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1
		AND identity_provider IN ('actions', 'github_mergeability', 'message') AND payload IS NOT NULL`, subscription.InboxID); got != 0 {
		t.Fatalf("owned rows retaining legacy JSON = %d", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1
		AND action_at IS NULL AND action_state IS NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("expired Action native content absent rows = %d, want 1", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 2
		AND mergeability_observed_at IS NOT NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("retained mergeability native content rows = %d, want 1", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 3
		AND message_body = 'private message' AND message_from_session = 'session-a'`, subscription.InboxID); got != 1 {
		t.Fatalf("retained Message native content rows = %d, want 1", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 4
		AND payload = '{"native":"github"}'::jsonb`, subscription.InboxID); got != 1 {
		t.Fatalf("GitHub JSON rows preserved = %d, want 1", got)
	}
	var phase string
	if err := db.QueryRowContext(ctx, `SELECT phase FROM notification_entry_legacy_cleanup WHERE singleton`).Scan(&phase); err != nil || phase != "cleaned" {
		t.Fatalf("cleanup checkpoint phase = %q, err = %v", phase, err)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_constraint WHERE conrelid = 'entry'::regclass
		AND conname::text = ANY($1::text[]) AND convalidated`, pq.Array(entryLegacyCleanupConstraints)); got != len(entryLegacyCleanupConstraints) {
		t.Fatalf("validated native and legacy-absence proofs = %d, want %d", got, len(entryLegacyCleanupConstraints))
	}
	if err := CleanupEntryLegacyContent(ctx, db, 2); err != nil {
		t.Fatalf("repeat completed cleanup: %v", err)
	}
}

func TestEntryLegacyCleanupRefusesRetainedContentGap(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 20); err != nil {
		t.Fatal(err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("missing-native"))
	action := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, action, entryPayloadForTest(t, action))
	f.exec(t, `UPDATE entry SET action_at = NULL, action_state = NULL,
		action_actor_kind = NULL, action_actor_namespace = NULL, action_actor_name = NULL,
		action_actor_issuer = NULL, action_actor_subject = NULL, payload = '{"historical":"only"}'::jsonb
		WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)
	if err := migrations.UpTo(ctx, db, 21); err != nil {
		t.Fatal(err)
	}
	err := CleanupEntryLegacyContent(ctx, db, 1)
	if err == nil || !strings.Contains(err.Error(), "no canonical native fields") {
		t.Fatalf("missing native content cleanup error = %v", err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1 AND payload IS NOT NULL`, subscription.InboxID); got != 1 {
		t.Fatal("cleanup discarded the only retained copy")
	}
}

func TestEntryLegacyCleanupBatchBoundAndFence(t *testing.T) {
	rows := []entryLegacyCleanupCandidate{{cursor: 1, estimatedBytes: entryLegacyCleanupBatchBytes/2 + 1}, {cursor: 2, estimatedBytes: entryLegacyCleanupBatchBytes/2 + 1}}
	if got := boundedEntryLegacyCleanupCandidates(rows); len(got) != 1 || got[0].cursor != 1 {
		t.Fatalf("byte-bounded batch = %+v", got)
	}
	rows[0].estimatedBytes = entryLegacyCleanupBatchBytes + 1
	if got := boundedEntryLegacyCleanupCandidates(rows); len(got) != 1 || got[0].cursor != 1 {
		t.Fatalf("oversize single-row batch = %+v", got)
	}
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 21); err != nil {
		t.Fatal(err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("retired-cleanup-fence"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	if _, err := db.ExecContext(ctx, `DROP TABLE notification_entry_legacy_cleanup`); err != nil {
		t.Fatal(err)
	}
	if err := CleanupEntryLegacyContent(ctx, db, 1); err == nil || !strings.Contains(err.Error(), "checkpoint") {
		t.Fatalf("stale cleanup after checkpoint retired = %v", err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1 AND action_at IS NOT NULL`, subscription.InboxID); got != 1 {
		t.Fatal("stale cleanup changed canonical Action content after checkpoint retirement")
	}
}

func TestEntryLegacyCleanupPersistsBatchCursorAndResumes(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 20); err != nil {
		t.Fatal(err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("resumable-owned-cleanup"))
	for sequence := int64(1); sequence <= 2; sequence++ {
		event := model.ActionsEvent{RequestID: uuid.New(), Sequence: sequence}
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	}
	f.exec(t, `UPDATE entry SET payload = '{"historical":"owned"}'::jsonb WHERE inbox_id = $1`, subscription.InboxID)
	if err := migrations.UpTo(ctx, db, 21); err != nil {
		t.Fatal(err)
	}
	conn, err := db.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	done, err := entryLegacyCleanupBatch(ctx, conn, 1)
	if err != nil || done {
		_ = conn.Close()
		t.Fatalf("first bounded batch: done=%t err=%v", done, err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	var lastInbox string
	var lastCursor int64
	var phase string
	if err := db.QueryRowContext(ctx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_entry_legacy_cleanup WHERE singleton`).Scan(&lastInbox, &lastCursor, &phase); err != nil {
		t.Fatal(err)
	}
	if lastInbox != subscription.InboxID.String() || lastCursor != 1 || phase != "cleaning" {
		t.Fatalf("persisted first-batch cursor = (%q, %d, %q)", lastInbox, lastCursor, phase)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1 AND payload IS NULL AND action_at IS NOT NULL`, subscription.InboxID); got != 1 {
		t.Fatal("first batch failed to clear only the first row's legacy JSON")
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 2 AND payload IS NOT NULL AND action_at IS NOT NULL`, subscription.InboxID); got != 1 {
		t.Fatal("first batch changed the second row")
	}
	if err := CleanupEntryLegacyContent(ctx, db, 1); err != nil {
		t.Fatalf("resume cleanup after first batch: %v", err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND payload IS NULL AND action_at IS NOT NULL`, subscription.InboxID); got != 2 {
		t.Fatalf("retained native rows after resume = %d, want 2", got)
	}
	if err := db.QueryRowContext(ctx, `SELECT phase FROM notification_entry_legacy_cleanup WHERE singleton`).Scan(&phase); err != nil || phase != "cleaned" {
		t.Fatalf("cleanup phase after resume = %q, err=%v", phase, err)
	}
}
