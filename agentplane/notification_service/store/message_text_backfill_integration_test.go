package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestBackfillMessageTextPreservesLegacyContentAndClearsExpiredText(t *testing.T) {
	f := newMessageTextBackfillFixture(t)
	inboxID := f.recipientInbox(t).ID
	fromSession := "source-e\u0301"
	expiredBelow := insertLegacyMessageForBackfill(t, f, inboxID, 1, "expired below prefix", ptr("old-source"), nil)
	expiredAtPrefix := insertLegacyMessageForBackfill(t, f, inboxID, 2, "expired at prefix", ptr("expired-source"), nil)
	f.exec(t, `UPDATE entry SET payload = NULL WHERE inbox_id = $1 AND cursor IN (1, 2)`, inboxID)
	first := insertLegacyMessageForBackfill(t, f, inboxID, 3, "cafe\u0301\r\n\x01", &fromSession, nil)
	second := insertLegacyMessageForBackfill(t, f, inboxID, 4, "no source label", nil, nil)
	nativeJSON := insertLegacyGitHubForBackfill(t, f, inboxID, 5)
	f.exec(t, `UPDATE inbox SET expired_through = 2 WHERE id = $1`, inboxID)
	f.migrateMessageTextBackfill(t)
	// Model an old cleanup worker that expired JSON but left newly introduced text behind. The cursor is below
	// expired_through to prove the backfill sweeps every native entry key rather than only retained prefixes.
	f.exec(t, `UPDATE entry SET message_body = 'stale body', message_from_session = 'stale source'
		WHERE inbox_id = $1 AND cursor = 1`, inboxID)

	beforeRows := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID)
	beforeMessages := f.count(t, `SELECT count(*) FROM message WHERE inbox_id = $1`, inboxID)
	if err := BackfillMessageText(t.Context(), f.db, 2); err != nil {
		t.Fatal(err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID); got != beforeRows {
		t.Fatalf("entry rows after backfill = %d, want %d", got, beforeRows)
	}
	if got := f.count(t, `SELECT count(*) FROM message WHERE inbox_id = $1`, inboxID); got != beforeMessages {
		t.Fatalf("message rows after backfill = %d, want %d", got, beforeMessages)
	}
	assertBackfilledMessageText(t, f, first, "cafe\u0301\r\n\x01", &fromSession)
	assertBackfilledMessageText(t, f, second, "no source label", nil)
	assertExpiredMessageText(t, f, expiredBelow)
	assertExpiredMessageText(t, f, expiredAtPrefix)
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 5
		AND payload::text = $2 AND message_body IS NULL AND message_from_session IS NULL`, inboxID, nativeJSON); got != 1 {
		t.Fatalf("native GitHub JSON row after backfill = %d matching rows, want 1", got)
	}
	assertMessageTextBackfillPhase(t, f, "backfilled")

	// Re-running after completion validates the durable state and preserves every row.
	if err := BackfillMessageText(t.Context(), f.db, 2); err != nil {
		t.Fatalf("repeat completed backfill: %v", err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID); got != beforeRows {
		t.Fatalf("entry rows after repeated backfill = %d, want %d", got, beforeRows)
	}
}

func TestBackfillMessageTextResumesAndRetainsMalformedRowsAndCursor(t *testing.T) {
	f := newMessageTextBackfillFixture(t)
	inboxID := f.recipientInbox(t).ID
	first := insertLegacyMessageForBackfill(t, f, inboxID, 1, "committed before interruption", nil, nil)
	malformed := insertLegacyMessageForBackfill(t, f, inboxID, 2, "unused", nil, []byte(`{"body":123}`))
	third := insertLegacyMessageForBackfill(t, f, inboxID, 3, "after malformed row", nil, nil)
	f.migrateMessageTextBackfill(t)
	beforeRows := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID)
	beforePayload := storedPayloadText(t, f.db, malformed)
	beforeDigest := messageDigest(t, f.db, malformed.messageID)

	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done, err := messageTextBackfillBatch(t.Context(), conn, 1)
	closeErr := conn.Close()
	if err != nil || closeErr != nil {
		t.Fatalf("commit first interrupted batch: %v, close: %v", err, closeErr)
	}
	if done {
		t.Fatal("first batch reported the end of a three-row scan")
	}
	assertMessageTextBackfillProgress(t, f, inboxID, 1, "backfilling")
	assertBackfilledMessageText(t, f, first, "committed before interruption", nil)

	err = BackfillMessageText(t.Context(), f.db, 1)
	if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("inbox %s entry 2", inboxID)) || !strings.Contains(err.Error(), "cannot unmarshal number") {
		t.Fatalf("malformed Message JSON error = %v, want its inbox/cursor and type error", err)
	}
	assertMessageTextBackfillProgress(t, f, inboxID, 1, "backfilling")
	assertBackfilledMessageText(t, f, first, "committed before interruption", nil)
	if got := storedPayloadText(t, f.db, malformed); got != beforePayload {
		t.Fatalf("malformed payload after rollback = %q, want unchanged %q", got, beforePayload)
	}
	if got := messageDigest(t, f.db, malformed.messageID); !bytes.Equal(got, beforeDigest) {
		t.Fatal("malformed row failure changed message.digest")
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID); got != beforeRows {
		t.Fatalf("entry rows after malformed payload = %d, want %d", got, beforeRows)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor IN (2, 3) AND message_body IS NULL`, inboxID); got != 2 {
		t.Fatalf("untouched rows after malformed batch = %d, want 2", got)
	}

	fixedPayload := legacyMessagePayloadForBackfill(t, "repaired and resumed", nil)
	digest := sha256.Sum256(fixedPayload)
	f.exec(t, `UPDATE entry SET payload = $2::jsonb WHERE inbox_id = $1 AND cursor = 2`, inboxID, string(fixedPayload))
	f.exec(t, `UPDATE message SET digest = $2 WHERE id = $1`, malformed.messageID, digest[:])
	malformed.payloadText = storedPayloadText(t, f.db, malformed)
	malformed.digest = digest[:]
	if err := BackfillMessageText(t.Context(), f.db, 1); err != nil {
		t.Fatalf("resume after repairing malformed source: %v", err)
	}
	assertBackfilledMessageText(t, f, first, "committed before interruption", nil)
	assertBackfilledMessageText(t, f, malformed, "repaired and resumed", nil)
	assertBackfilledMessageText(t, f, third, "after malformed row", nil)
	assertMessageTextBackfillPhase(t, f, "backfilled")
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID); got != beforeRows {
		t.Fatalf("entry rows after successful resume = %d, want %d", got, beforeRows)
	}
}

func TestBackfillMessageTextValidationResetsWhenLegacyRowChangesBehindCursor(t *testing.T) {
	f := newMessageTextBackfillFixture(t)
	inboxID := f.recipientInbox(t).ID
	first := insertLegacyMessageForBackfill(t, f, inboxID, 1, "missed legacy row", nil, nil)
	second := insertLegacyMessageForBackfill(t, f, inboxID, 2, "later legacy row", nil, nil)
	f.migrateMessageTextBackfill(t)
	beforeRows := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID)

	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done, err := messageTextBackfillBatch(t.Context(), conn, 1)
	if err != nil || done {
		conn.Close()
		t.Fatalf("commit first backfill batch: done %t, err %v", done, err)
	}
	assertMessageTextBackfillProgress(t, f, inboxID, 1, "backfilling")
	// Simulate a writer that failed the dual-write gate after this key had already been scanned.
	f.exec(t, `UPDATE entry SET message_body = NULL WHERE inbox_id = $1 AND cursor = 1`, inboxID)
	for {
		done, err = messageTextBackfillBatch(t.Context(), conn, 1)
		if err != nil {
			conn.Close()
			t.Fatalf("finish scan after missed legacy row: %v", err)
		}
		if done {
			break
		}
	}
	assertMessageTextBackfillPhase(t, f, "scanned")
	if err := validateMessageTextBackfill(t.Context(), conn); err == nil || !strings.Contains(err.Error(), "retained Message JSON remains without text columns") {
		conn.Close()
		t.Fatalf("validation after behind-cursor legacy row = %v, want incomplete-backfill error", err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	assertMessageTextBackfillPhase(t, f, "backfilling")
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_inbox_id::text, last_cursor
		FROM notification_message_text_backfill WHERE singleton`).Scan(&lastInboxID, &lastCursor); err != nil {
		t.Fatal(err)
	}
	if lastInboxID.Valid || lastCursor.Valid {
		t.Fatalf("validation failure retained stale progress cursor (%q, %d)", lastInboxID.String, lastCursor.Int64)
	}
	if got := storedPayloadText(t, f.db, first); got != first.payloadText {
		t.Fatalf("behind-cursor legacy JSON changed: got %q, want %q", got, first.payloadText)
	}
	if got := messageDigest(t, f.db, first.messageID); !bytes.Equal(got, first.digest) {
		t.Fatal("validation failure changed behind-cursor message.digest")
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID); got != beforeRows {
		t.Fatalf("entry rows after failed final validation = %d, want %d", got, beforeRows)
	}

	if err := BackfillMessageText(t.Context(), f.db, 1); err != nil {
		t.Fatalf("resume from reset progress: %v", err)
	}
	assertBackfilledMessageText(t, f, first, "missed legacy row", nil)
	assertBackfilledMessageText(t, f, second, "later legacy row", nil)
	assertMessageTextBackfillPhase(t, f, "backfilled")
}

func TestBackfillMessageTextUsesSerializedByteBudgetAndProcessesOversizedDocumentAlone(t *testing.T) {
	f := newMessageTextBackfillFixture(t)
	inboxID := f.recipientInbox(t).ID
	// The body contains valid one-byte control characters. JSON escapes each as six ASCII bytes, so its valid
	// legacy document exceeds the serialized-byte budget even though the body itself is under the 256 KiB limit.
	largeBody := strings.Repeat("\x01", 180_000)
	large := insertLegacyMessageForBackfill(t, f, inboxID, 1, largeBody, nil, nil)
	small := insertLegacyMessageForBackfill(t, f, inboxID, 2, "small", nil, nil)
	f.migrateMessageTextBackfill(t)
	var serializedBytes, storedBytes int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT octet_length(payload::text), pg_column_size(payload)
		FROM entry WHERE inbox_id = $1 AND cursor = 1`, inboxID).Scan(&serializedBytes, &storedBytes); err != nil {
		t.Fatal(err)
	}
	if serializedBytes <= messageTextBackfillBatchBytes {
		t.Fatalf("serialized valid payload size = %d, want larger than %d", serializedBytes, messageTextBackfillBatchBytes)
	}
	if storedBytes >= serializedBytes {
		t.Fatalf("fixture payload stored size = %d, serialized size = %d; want compression to demonstrate raw-byte budgeting", storedBytes, serializedBytes)
	}
	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done, err := messageTextBackfillBatch(t.Context(), conn, 16)
	closeErr := conn.Close()
	if err != nil || closeErr != nil {
		t.Fatalf("process oversized Message batch: %v, close: %v", err, closeErr)
	}
	if done {
		t.Fatal("oversized first batch reported scan completion")
	}
	assertMessageTextBackfillProgress(t, f, inboxID, 1, "backfilling")
	assertBackfilledMessageText(t, f, large, largeBody, nil)
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 2 AND message_body IS NULL`, inboxID); got != 1 {
		t.Fatalf("small row after oversized first batch = %d rows without text, want 1", got)
	}
	if err := BackfillMessageText(t.Context(), f.db, 16); err != nil {
		t.Fatalf("finish byte-bounded backfill: %v", err)
	}
	assertBackfilledMessageText(t, f, small, "small", nil)
	assertMessageTextBackfillPhase(t, f, "backfilled")
}

func TestBackfillMessageTextDoesNotResurrectPayloadExpiredWhileLoading(t *testing.T) {
	f := newMessageTextBackfillFixture(t)
	inboxID := f.recipientInbox(t).ID
	message := insertLegacyMessageForBackfill(t, f, inboxID, 1, "must expire", ptr("source"), nil)
	f.migrateMessageTextBackfill(t)
	f.exec(t, `UPDATE inbox SET expired_through = 1 WHERE id = $1`, inboxID)
	cleanupTx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := cleanupTx.ExecContext(t.Context(), `UPDATE entry SET payload = NULL, message_body = NULL, message_from_session = NULL
		WHERE inbox_id = $1 AND cursor = 1`, inboxID); err != nil {
		cleanupTx.Rollback()
		t.Fatal(err)
	}

	conn, err := f.db.Conn(t.Context())
	if err != nil {
		cleanupTx.Rollback()
		t.Fatal(err)
	}
	type batchResult struct {
		done bool
		err  error
	}
	result := make(chan batchResult, 1)
	go func() {
		done, err := messageTextBackfillBatch(t.Context(), conn, 1)
		result <- batchResult{done: done, err: err}
	}()
	deadline := time.Now().Add(2 * time.Second)
	var waiting bool
	for time.Now().Before(deadline) {
		if err := f.db.QueryRowContext(t.Context(), `SELECT EXISTS (
			SELECT 1 FROM pg_stat_activity WHERE pid <> pg_backend_pid()
				AND wait_event_type = 'Lock' AND query LIKE '%FOR UPDATE OF e%'
		)`).Scan(&waiting); err != nil {
			cleanupTx.Rollback()
			conn.Close()
			t.Fatal(err)
		}
		if waiting {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !waiting {
		cleanupTx.Rollback()
		conn.Close()
		t.Fatal("backfill did not wait on the cleanup row lock")
	}
	if err := cleanupTx.Commit(); err != nil {
		conn.Close()
		t.Fatal(err)
	}
	batch := <-result
	if batch.err != nil || batch.done {
		conn.Close()
		t.Fatalf("batch after concurrent expiry = done %t, err %v", batch.done, batch.err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if err := BackfillMessageText(t.Context(), f.db, 1); err != nil {
		t.Fatalf("validate after concurrent expiry: %v", err)
	}
	assertExpiredMessageText(t, f, message)
	assertMessageTextBackfillPhase(t, f, "backfilled")
}

func TestBackfillMessageTextUsesNativeKeysetAndSetBasedPlans(t *testing.T) {
	f := newMessageTextBackfillFixture(t)
	inboxID := f.recipientInbox(t).ID
	const rowCount = 100_000
	if _, err := f.db.ExecContext(t.Context(), `INSERT INTO entry (inbox_id, cursor, event, payload, created_at,
		identity_provider, message_id, message_sender_namespace, message_sender_name)
		SELECT $1, sequence,
			jsonb_build_object('provider', 'message', 'message_id', md5(sequence::text)::uuid,
				'sender', jsonb_build_object('namespace', 'plan-sender-ns', 'name', 'plan-sender')),
			jsonb_build_object('body', 'plan body'), now(), 'message', md5(sequence::text)::uuid,
			'plan-sender-ns', 'plan-sender'
		FROM generate_series(1, $2) AS series(sequence)`, inboxID, rowCount); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE inbox SET last_cursor = $2 WHERE id = $1`, inboxID, rowCount)
	f.exec(t, `UPDATE entry SET payload = NULL WHERE inbox_id = $1 AND cursor <= $2`, inboxID, messageTextBackfillBatchSize)
	f.exec(t, `UPDATE inbox SET expired_through = $2 WHERE id = $1`, inboxID, messageTextBackfillBatchSize)
	f.migrateMessageTextBackfill(t)
	f.exec(t, `UPDATE entry SET message_body = 'stale plan body' WHERE inbox_id = $1 AND cursor <= $2`, inboxID, messageTextBackfillBatchSize)
	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := checkMessageTextBackfillPlans(t.Context(), conn, inboxID); err != nil {
		t.Fatal(err)
	}
}

func TestBackfillMessageTextCompletesAnEmptyFreshDatabase(t *testing.T) {
	db, ctx := newMessageTextBackfillDatabase(t, 17)
	if err := BackfillMessageText(ctx, db, 16); err != nil {
		t.Fatal(err)
	}
	assertMessageTextBackfillPhase(t, fixtureForDatabase(t, db, defaultQuotas()), "backfilled")
	assertGooseVersionApplied(t, db, 17)
}

func newMessageTextBackfillFixture(t *testing.T) fixture {
	t.Helper()
	db, ctx := newMessageTextBackfillDatabase(t, 15)
	f := fixtureForDatabase(t, db, messageQuotas())
	f.openInbox(t)
	return f
}

func newMessageTextBackfillDatabase(t *testing.T, version int64) (*sql.DB, context.Context) {
	t.Helper()
	db, ctx := newStorageIntegrationDatabase(t)
	if _, err := db.ExecContext(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset cloned database schema: %v", err)
	}
	if err := migrations.UpTo(ctx, db, version); err != nil {
		t.Fatalf("apply schema through Notification migration %05d: %v", version, err)
	}
	return db, ctx
}

func (f fixture) migrateMessageTextBackfill(t *testing.T) {
	t.Helper()
	if err := migrations.UpTo(t.Context(), f.db, 17); err != nil {
		t.Fatalf("apply Message text expansion and backfill migrations through 00017: %v", err)
	}
}

type legacyMessageBackfillRow struct {
	inboxID     uuid.UUID
	cursor      int64
	messageID   uuid.UUID
	sender      ServiceAccount
	idempotency string
	eventJSON   string
	payloadText string
	digest      []byte
}

func insertLegacyMessageForBackfill(
	t *testing.T,
	f fixture,
	inboxID uuid.UUID,
	cursor int64,
	body string,
	fromSession *string,
	payloadOverride []byte,
) legacyMessageBackfillRow {
	t.Helper()
	messageID := uuid.New()
	sender := ServiceAccount{Namespace: "backfill-sender-ns", Name: fmt.Sprintf("backfill-sender-%d", cursor)}
	idempotency := fmt.Sprintf("backfill-key-%d", cursor)
	eventJSON, err := json.Marshal(MessageEvent{MessageID: messageID, Sender: sender})
	if err != nil {
		t.Fatal(err)
	}
	payload := payloadOverride
	if payload == nil {
		payload = legacyMessagePayloadForBackfill(t, body, fromSession)
	}
	digest := sha256.Sum256(payload)
	f.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at,
		identity_provider, message_id, message_sender_namespace, message_sender_name)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, now(), 'message', $5, $6, $7)`,
		inboxID, cursor, string(eventJSON), string(payload), messageID, sender.Namespace, sender.Name)
	f.exec(t, `INSERT INTO message (id, inbox_id, cursor, sender_namespace, sender_name, idempotency_key, digest)
		VALUES ($1, $2, $3, $4, $5, $6, $7)`, messageID, inboxID, cursor, sender.Namespace, sender.Name, idempotency, digest[:])
	f.exec(t, `UPDATE inbox SET last_cursor = GREATEST(last_cursor, $2) WHERE id = $1`, inboxID, cursor)
	var storedEvent, storedPayload sql.NullString
	if err := f.db.QueryRowContext(t.Context(), `SELECT event::text, payload::text FROM entry WHERE inbox_id = $1 AND cursor = $2`, inboxID, cursor).
		Scan(&storedEvent, &storedPayload); err != nil {
		t.Fatal(err)
	}
	if !storedEvent.Valid || !storedPayload.Valid {
		t.Fatalf("new legacy Message event/payload for cursor %d has SQL NULL: event=%t payload=%t", cursor, storedEvent.Valid, storedPayload.Valid)
	}
	return legacyMessageBackfillRow{
		inboxID: inboxID, cursor: cursor, messageID: messageID, sender: sender, idempotency: idempotency,
		eventJSON: storedEvent.String, payloadText: storedPayload.String, digest: digest[:],
	}
}

func insertLegacyGitHubForBackfill(t *testing.T, f fixture, inboxID uuid.UUID, cursor int64) string {
	t.Helper()
	event := GitHubEvent{AppID: 17, DeliveryID: uuid.New(), RepositoryID: 29, Event: EventPullRequest, Action: ptr("opened")}
	eventJSON, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	const payload = `{"native":["json",true,17]}`
	f.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at,
		identity_provider, github_app_id, github_delivery_id, github_repository_id, github_event, github_action)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, now(), 'github', $5, $6, $7, $8, $9)`,
		inboxID, cursor, string(eventJSON), payload, event.AppID, event.DeliveryID, event.RepositoryID, string(event.Event), *event.Action)
	f.exec(t, `UPDATE inbox SET last_cursor = GREATEST(last_cursor, $2) WHERE id = $1`, inboxID, cursor)
	var stored sql.NullString
	if err := f.db.QueryRowContext(t.Context(), `SELECT payload::text FROM entry WHERE inbox_id = $1 AND cursor = $2`, inboxID, cursor).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if !stored.Valid {
		t.Fatalf("native GitHub payload at cursor %d is SQL NULL", cursor)
	}
	return stored.String
}

func legacyMessagePayloadForBackfill(t *testing.T, body string, fromSession *string) []byte {
	t.Helper()
	payload, err := json.Marshal(struct {
		Body        string  `json:"body"`
		FromSession *string `json:"from_session,omitempty"`
	}{Body: body, FromSession: fromSession})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func (f fixture) assertMessageBackfillEntry(t *testing.T, row legacyMessageBackfillRow, wantBody string, wantFromSession *string) {
	t.Helper()
	var body, fromSession, payload sql.NullString
	var event, provider, senderNamespace, senderName string
	var messageID uuid.UUID
	if err := f.db.QueryRowContext(t.Context(), `SELECT message_body, message_from_session, payload::text, event::text,
		identity_provider, message_id, message_sender_namespace, message_sender_name
		FROM entry WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor).
		Scan(&body, &fromSession, &payload, &event, &provider, &messageID, &senderNamespace, &senderName); err != nil {
		t.Fatal(err)
	}
	if !body.Valid || body.String != wantBody {
		t.Errorf("cursor %d body = (%t, %q), want (%t, %q)", row.cursor, body.Valid, body.String, true, wantBody)
	}
	if wantFromSession == nil {
		if fromSession.Valid {
			t.Errorf("cursor %d absent from_session = %q, want SQL NULL", row.cursor, fromSession.String)
		}
	} else if !fromSession.Valid || fromSession.String != *wantFromSession {
		t.Errorf("cursor %d from_session = (%t, %q), want (%t, %q)", row.cursor, fromSession.Valid, fromSession.String, true, *wantFromSession)
	}
	if !payload.Valid || payload.String != row.payloadText {
		t.Errorf("cursor %d legacy JSON = (%t, %q), want preserved (%t, %q)", row.cursor, payload.Valid, payload.String, true, row.payloadText)
	}
	if event != row.eventJSON || provider != "message" || messageID != row.messageID ||
		senderNamespace != row.sender.Namespace || senderName != row.sender.Name {
		t.Errorf("cursor %d identity changed: event=%q provider=%q id=%s sender=%s/%s", row.cursor,
			event, provider, messageID, senderNamespace, senderName)
	}
	if digest := messageDigest(t, f.db, row.messageID); !bytes.Equal(digest, row.digest) {
		t.Errorf("cursor %d message.digest changed", row.cursor)
	}
}

func assertBackfilledMessageText(t *testing.T, f fixture, row legacyMessageBackfillRow, body string, fromSession *string) {
	t.Helper()
	f.assertMessageBackfillEntry(t, row, body, fromSession)
}

func assertExpiredMessageText(t *testing.T, f fixture, row legacyMessageBackfillRow) {
	t.Helper()
	var payload, body, fromSession sql.NullString
	if err := f.db.QueryRowContext(t.Context(), `SELECT payload::text, message_body, message_from_session
		FROM entry WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor).Scan(&payload, &body, &fromSession); err != nil {
		t.Fatal(err)
	}
	if payload.Valid || body.Valid || fromSession.Valid {
		t.Errorf("expired cursor %d retained payload=%t body=%t from_session=%t; all must be SQL NULL",
			row.cursor, payload.Valid, body.Valid, fromSession.Valid)
	}
	if digest := messageDigest(t, f.db, row.messageID); !bytes.Equal(digest, row.digest) {
		t.Errorf("expired cursor %d message.digest changed", row.cursor)
	}
}

func messageDigest(t *testing.T, db *sql.DB, messageID uuid.UUID) []byte {
	t.Helper()
	var digest []byte
	if err := db.QueryRowContext(t.Context(), `SELECT digest FROM message WHERE id = $1`, messageID).Scan(&digest); err != nil {
		t.Fatal(err)
	}
	return digest
}

func storedPayloadText(t *testing.T, db *sql.DB, row legacyMessageBackfillRow) string {
	t.Helper()
	var payload sql.NullString
	if err := db.QueryRowContext(t.Context(), `SELECT payload::text FROM entry WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	if !payload.Valid {
		t.Fatalf("Message payload at cursor %d is SQL NULL", row.cursor)
	}
	return payload.String
}

func assertMessageTextBackfillPhase(t *testing.T, f fixture, want string) {
	t.Helper()
	var phase string
	if err := f.db.QueryRowContext(t.Context(), `SELECT phase FROM notification_message_text_backfill WHERE singleton`).Scan(&phase); err != nil {
		t.Fatal(err)
	}
	if phase != want {
		t.Fatalf("Message text backfill phase = %q, want %q", phase, want)
	}
}

func assertMessageTextBackfillProgress(t *testing.T, f fixture, inboxID uuid.UUID, cursor int64, phase string) {
	t.Helper()
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	var gotPhase string
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_message_text_backfill WHERE singleton`).Scan(&lastInboxID, &lastCursor, &gotPhase); err != nil {
		t.Fatal(err)
	}
	if !lastInboxID.Valid || lastInboxID.String != inboxID.String() || !lastCursor.Valid || lastCursor.Int64 != cursor || gotPhase != phase {
		t.Fatalf("Message text progress = (%q, %d, %q), want (%s, %d, %q)",
			lastInboxID.String, lastCursor.Int64, gotPhase, inboxID, cursor, phase)
	}
}

func checkMessageTextBackfillPlans(ctx context.Context, conn *sql.Conn, inboxID uuid.UUID) error {
	if _, err := conn.ExecContext(ctx, `ANALYZE entry`); err != nil {
		return fmt.Errorf("analyze entry before checking Message text plans: %w", err)
	}
	var estimatedRows float64
	if err := conn.QueryRowContext(ctx, `SELECT reltuples FROM pg_class WHERE oid = 'entry'::regclass`).Scan(&estimatedRows); err != nil {
		return err
	}
	if estimatedRows < 100_000 {
		return fmt.Errorf("Message text plan fixture has only %.0f estimated rows; want at least 100000", estimatedRows)
	}
	var cursor int64
	if err := conn.QueryRowContext(ctx, `SELECT cursor FROM entry WHERE inbox_id = $1 ORDER BY cursor OFFSET 1000 LIMIT 1`, inboxID).Scan(&cursor); err != nil {
		return fmt.Errorf("read later Message keyset cursor: %w", err)
	}
	ids := make([]string, messageTextBackfillBatchSize)
	cursors := make([]int64, messageTextBackfillBatchSize)
	idsForExpired := make([]string, messageTextBackfillBatchSize)
	cursorsForExpired := make([]int64, messageTextBackfillBatchSize)
	bodies := make([]string, messageTextBackfillBatchSize)
	hasFromSession := make([]bool, messageTextBackfillBatchSize)
	fromSessions := make([]string, messageTextBackfillBatchSize)
	for i := 0; i < messageTextBackfillBatchSize; i++ {
		ids[i] = inboxID.String()
		cursors[i] = int64(messageTextBackfillBatchSize + i + 1)
		bodies[i] = "plan body"
		idsForExpired[i] = inboxID.String()
		cursorsForExpired[i] = int64(i + 1)
	}
	idsForUpdate := pq.Array(ids)
	cursorsForUpdate := pq.Array(cursors)
	connTx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer connTx.Rollback()
	plans := []struct {
		name          string
		query         string
		args          []any
		orderedKeyset bool
	}{
		{name: "initial keyset scan", query: messageTextBackfillFromStart, args: []any{512}, orderedKeyset: true},
		{name: "later keyset scan", query: messageTextBackfillAfter, args: []any{inboxID, cursor, 512}, orderedKeyset: true},
		{name: "selected JSON load", query: messageTextBackfillLoad, args: []any{idsForUpdate, cursorsForUpdate}},
		{name: "bulk text update", query: messageTextBackfillUpdate, args: []any{
			idsForUpdate, cursorsForUpdate,
			pq.Array(bodies), pq.Array(hasFromSession), pq.Array(fromSessions),
		}},
		{
			name: "expired text cleanup update", query: messageTextBackfillExpiredUpdate,
			args: []any{pq.Array(idsForExpired), pq.Array(cursorsForExpired)},
		},
	}
	for _, check := range plans {
		plan, err := explainLines(ctx, connTx, check.query, check.args...)
		if err != nil {
			return fmt.Errorf("explain Message text %s: %w", check.name, err)
		}
		if !strings.Contains(plan, "entry_pkey") || strings.Contains(plan, "Seq Scan") {
			return fmt.Errorf("Message text %s plan must use entry_pkey without a table scan:\n%s", check.name, plan)
		}
		if check.orderedKeyset && strings.Contains(plan, "Sort") {
			return fmt.Errorf("Message text %s plan must preserve native primary-key order without a sort:\n%s", check.name, plan)
		}
	}
	return nil
}
