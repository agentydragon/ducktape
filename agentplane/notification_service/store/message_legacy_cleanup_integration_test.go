package store

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/binary"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

const messageLegacyCleanupMigrationVersion = 20

func TestCleanupMessageLegacyContentPreservesCanonicalRowsAndOtherProviders(t *testing.T) {
	f, subscription, creation := newMessageLegacyCleanupFixture(t)
	expiredFrom := "expired-source"
	expired := sendLegacyMessageForCleanup(t, f, "expired", "expired\x01 body", &expiredFrom)
	retainedFrom := "source-e\u0301"
	retained := sendLegacyMessageForCleanup(t, f, "retained", "cafe\u0301\r\n\x01", &retainedFrom)
	withoutSource := sendLegacyMessageForCleanup(t, f, "no-source", "retained without a source", nil)
	f.exec(t, `UPDATE inbox SET expired_through = $2 WHERE id = $1`, subscription.InboxID, expired.cursor)

	action := ActionsEvent{RequestID: creation.Source.(ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, action, entryPayloadForTest(t, action))
	github := GitHubEvent{AppID: 17, DeliveryID: uuid.New(), RepositoryID: 29, Event: EventPullRequest}
	const nativeGitHubJSON = `{"native":["json",true,17]}`
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, github, nativeGitHubJSON)
	mergeability := GitHubMergeabilityEvent{AppID: 17, RepositoryID: 29, PullRequestNumber: 7, Sequence: 3}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, mergeability, entryPayloadForTest(t, mergeability))
	otherProviders := readCleanupProviderRows(t, f.db, subscription.InboxID)
	entriesBefore := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, subscription.InboxID)
	messagesBefore := f.count(t, `SELECT count(*) FROM message WHERE inbox_id = $1`, subscription.InboxID)

	applyMessageLegacyCleanupMigration(t, f.db)
	var validated bool
	if err := f.db.QueryRowContext(t.Context(), `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname = 'entry_message_legacy_content_absent'`).Scan(&validated); err != nil {
		t.Fatalf("read new check validation state: %v", err)
	}
	if validated {
		t.Fatal("legacy-content constraint scanned historical rows during migration")
	}
	// The NOT VALID checks reject new legacy writes while leaving historical rows for the offline cleanup.
	cleanedMessage := f.message(newOwner(), "post-proof", "new canonical Message")
	cleaned, err := f.Send(t.Context(), cleanedMessage)
	if err != nil {
		t.Fatalf("TEXT-only Message write after proof migration: %v", err)
	}
	if _, err := f.db.ExecContext(t.Context(), `UPDATE entry SET payload = '{}'::jsonb WHERE message_id = $1`, cleaned.ID); err == nil {
		t.Fatal("Message JSON write succeeded after the legacy-content proof was installed")
	} else {
		var pgErr *pq.Error
		if !errors.As(err, &pgErr) || pgErr.Constraint != "entry_message_legacy_content_absent" {
			t.Fatalf("Message JSON write error = %v, want entry_message_legacy_content_absent", err)
		}
	}

	if err := CleanupMessageLegacyContent(t.Context(), f.db, 2); err != nil {
		t.Fatal(err)
	}

	assertLegacyCleanupMessage(t, f, expired, false, false, false)
	assertLegacyCleanupMessage(t, f, retained, false, true, true)
	assertLegacyCleanupMessage(t, f, withoutSource, false, true, false)
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, subscription.InboxID); got != entriesBefore+1 {
		t.Fatalf("entry rows after cleanup = %d, want %d", got, entriesBefore+1)
	}
	if got := f.count(t, `SELECT count(*) FROM message WHERE inbox_id = $1`, subscription.InboxID); got != messagesBefore+1 {
		t.Fatalf("message rows after cleanup = %d, want %d", got, messagesBefore+1)
	}
	assertCleanupProviderRows(t, f.db, subscription.InboxID, otherProviders)
	assertMessageLegacyCleanupConstraintsValidated(t, f.db)
	assertMessageLegacyCleanupPhase(t, f.db, "cleaned")
}

func TestCleanupMessageLegacyContentResumesAndIsIdempotent(t *testing.T) {
	f, subscription, _ := newMessageLegacyCleanupFixture(t)
	first := sendLegacyMessageForCleanup(t, f, "first", "first retained", nil)
	secondFrom := "session-label"
	second := sendLegacyMessageForCleanup(t, f, "second", "second retained", &secondFrom)
	third := sendLegacyMessageForCleanup(t, f, "third", "third retained", nil)
	applyMessageLegacyCleanupMigration(t, f.db)

	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done, err := messageLegacyCleanupBatch(t.Context(), conn, 1)
	if err != nil || done {
		_ = conn.Close()
		t.Fatalf("first interrupted cleanup batch: done=%t err=%v", done, err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	assertCleanupProgress(t, f.db, subscription.InboxID, first.cursor, "cleaning")
	assertLegacyCleanupMessage(t, f, first, false, true, false)
	assertLegacyCleanupMessage(t, f, second, true, true, true)

	if err := CleanupMessageLegacyContent(t.Context(), f.db, 1); err != nil {
		t.Fatalf("resume interrupted cleanup: %v", err)
	}
	for _, row := range []cleanupMessage{first, second, third} {
		assertLegacyCleanupMessage(t, f, row, false, true, row.fromSession != nil)
	}
	assertMessageLegacyCleanupPhase(t, f.db, "cleaned")

	if err := CleanupMessageLegacyContent(t.Context(), f.db, 1); err != nil {
		t.Fatalf("repeat completed cleanup: %v", err)
	}
	for _, row := range []cleanupMessage{first, second, third} {
		assertLegacyCleanupMessage(t, f, row, false, true, row.fromSession != nil)
	}
	assertMessageLegacyCleanupPhase(t, f.db, "cleaned")
}

func TestCleanupMessageLegacyContentRetainsLegacySourceWhenCanonicalBodyIsMissing(t *testing.T) {
	f, _, _ := newMessageLegacyCleanupFixture(t)
	row := sendLegacyMessageForCleanup(t, f, "missing-body", "repair me", nil)
	// A pre-existing row with missing canonical text is allowed through NOT VALID expansion constraints.
	f.exec(t, `UPDATE entry SET message_body = NULL WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor)
	applyMessageLegacyCleanupMigration(t, f.db)

	err := CleanupMessageLegacyContent(t.Context(), f.db, 1)
	if err == nil || !strings.Contains(err.Error(), "no canonical body") {
		t.Fatalf("cleanup with missing canonical body = %v, want fail-safe error", err)
	}
	assertMessageLegacyCleanupPhase(t, f.db, "cleaning")
	assertLegacyCleanupMessage(t, f, row, true, false, false)

	// Repair the canonical body and retire the conflicting representations in one write that satisfies the new
	// immediately-enforced proof; the next command pass completes verification and validation.
	f.exec(t, `UPDATE entry SET message_body = $3, payload = NULL, typed_payload = NULL
		WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor, "repair me")
	if err := CleanupMessageLegacyContent(t.Context(), f.db, 1); err != nil {
		t.Fatalf("resume after restoring canonical body: %v", err)
	}
	assertLegacyCleanupMessage(t, f, row, false, true, false)
}

func TestCleanupMessageLegacyContentValidationFailureResetsFullScan(t *testing.T) {
	f, _, _ := newMessageLegacyCleanupFixture(t)
	first := sendLegacyMessageForCleanup(t, f, "first", "already cleaned", nil)
	applyMessageLegacyCleanupMigration(t, f.db)
	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done, err := messageLegacyCleanupBatch(t.Context(), conn, 1)
	if err != nil || done {
		_ = conn.Close()
		t.Fatalf("clean first row before final scan: done=%t err=%v", done, err)
	}
	done, err = messageLegacyCleanupBatch(t.Context(), conn, 1)
	if err != nil || !done {
		_ = conn.Close()
		t.Fatalf("mark completed key scan: done=%t err=%v", done, err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	assertMessageLegacyCleanupPhase(t, f.db, "scanned")

	// Model an old writer bypassing the database proof after the scan. A failed verification must reset the
	// checkpoint and keep this late row's canonical and historical content intact for the next full pass.
	late := sendLegacyMessageForCleanup(t, f, "late", "arrived after scan", nil)
	f.exec(t, `ALTER TABLE entry DROP CONSTRAINT entry_message_legacy_content_absent`)
	f.exec(t, `ALTER TABLE entry DROP CONSTRAINT entry_typed_payload_source_canonical`)
	writeLegacyMessageCleanupPayload(t, f, late, "arrived after scan", nil)
	f.exec(t, `ALTER TABLE entry ADD CONSTRAINT entry_message_legacy_content_absent CHECK (
		identity_provider IS DISTINCT FROM 'message' OR (payload IS NULL AND typed_payload IS NULL)
	) NOT VALID`)
	f.exec(t, `ALTER TABLE entry ADD CONSTRAINT entry_typed_payload_source_canonical CHECK (
		typed_payload IS NULL OR
		(identity_provider IS NOT NULL AND identity_provider IN ('actions', 'github_mergeability'))
	) NOT VALID`)
	conn, err = f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	err = validateMessageLegacyCleanup(t.Context(), conn)
	closeErr := conn.Close()
	if err == nil || !strings.Contains(err.Error(), "Message JSON or protobuf remains") || closeErr != nil {
		t.Fatalf("validation with late legacy row: error=%v close=%v, want reset error", err, closeErr)
	}
	assertMessageLegacyCleanupPhase(t, f.db, "cleaning")
	assertCleanupCursorEmpty(t, f.db)
	assertLegacyCleanupMessage(t, f, first, false, true, false)
	assertLegacyCleanupMessage(t, f, late, true, true, false)

	if err := CleanupMessageLegacyContent(t.Context(), f.db, 1); err != nil {
		t.Fatalf("retry full cleanup after validation reset: %v", err)
	}
	assertLegacyCleanupMessage(t, f, first, false, true, false)
	assertLegacyCleanupMessage(t, f, late, false, true, false)
}

func TestCleanupMessageLegacyContentRequiresEveryProofConstraint(t *testing.T) {
	f, _, _ := newMessageLegacyCleanupFixture(t)
	row := sendLegacyMessageForCleanup(t, f, "missing-proof", "preserve canonical text", nil)
	applyMessageLegacyCleanupMigration(t, f.db)

	// Model an incomplete schema rollout after the row scan. A missing proof must never be treated as if it were
	// already validated, and the checkpoint must remain retryable rather than reaching cleaned.
	f.exec(t, `ALTER TABLE entry DROP CONSTRAINT entry_message_legacy_content_absent`)
	err := CleanupMessageLegacyContent(t.Context(), f.db, 1)
	if err == nil || !strings.Contains(err.Error(), "entry_message_legacy_content_absent") {
		t.Fatalf("cleanup with missing proof constraint = %v, want missing-constraint error", err)
	}
	assertMessageLegacyCleanupPhase(t, f.db, "cleaning")
	assertCleanupCursorEmpty(t, f.db)
	assertLegacyCleanupMessage(t, f, row, false, true, false)

	f.exec(t, `ALTER TABLE entry ADD CONSTRAINT entry_message_legacy_content_absent CHECK (
		identity_provider IS DISTINCT FROM 'message' OR (payload IS NULL AND typed_payload IS NULL)
	) NOT VALID`)
	if err := CleanupMessageLegacyContent(t.Context(), f.db, 1); err != nil {
		t.Fatalf("retry after restoring missing proof constraint: %v", err)
	}
	assertMessageLegacyCleanupPhase(t, f.db, "cleaned")
	assertMessageLegacyCleanupConstraintsValidated(t, f.db)
	assertLegacyCleanupMessage(t, f, row, false, true, false)
}

func TestCleanupMessageLegacyContentDoesNotResurrectExpiredTextWhileRowLocked(t *testing.T) {
	f, subscription, _ := newMessageLegacyCleanupFixture(t)
	row := sendLegacyMessageForCleanup(t, f, "expiry-race", "expires during cleanup", ptr("source"))
	applyMessageLegacyCleanupMigration(t, f.db)

	expiryTx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	fatalRollback := func() { _ = expiryTx.Rollback() }
	if _, err := expiryTx.ExecContext(t.Context(), `UPDATE inbox SET expired_through = $2 WHERE id = $1`, subscription.InboxID, row.cursor); err != nil {
		fatalRollback()
		t.Fatal(err)
	}
	if _, err := expiryTx.ExecContext(t.Context(), `UPDATE entry SET payload = NULL, typed_payload = NULL,
		message_body = NULL, message_from_session = NULL WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor); err != nil {
		fatalRollback()
		t.Fatal(err)
	}

	conn, err := f.db.Conn(t.Context())
	if err != nil {
		fatalRollback()
		t.Fatal(err)
	}
	type batchResult struct {
		done bool
		err  error
	}
	result := make(chan batchResult, 1)
	go func() {
		done, err := messageLegacyCleanupBatch(t.Context(), conn, 1)
		result <- batchResult{done: done, err: err}
	}()
	deadline := time.Now().Add(2 * time.Second)
	var waiting bool
	for time.Now().Before(deadline) {
		if err := f.db.QueryRowContext(t.Context(), `SELECT EXISTS (
			SELECT 1 FROM pg_stat_activity WHERE pid <> pg_backend_pid()
				AND wait_event_type = 'Lock' AND query LIKE 'WITH batch AS (%'
		)`).Scan(&waiting); err != nil {
			fatalRollback()
			_ = conn.Close()
			t.Fatal(err)
		}
		if waiting {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !waiting {
		fatalRollback()
		_ = conn.Close()
		t.Fatal("cleanup batch did not wait for the expiry row lock")
	}
	if err := expiryTx.Commit(); err != nil {
		_ = conn.Close()
		t.Fatal(err)
	}
	batch := <-result
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if batch.err != nil || batch.done {
		t.Fatalf("cleanup batch after expiry commit: done=%t err=%v", batch.done, batch.err)
	}
	if err := CleanupMessageLegacyContent(t.Context(), f.db, 1); err != nil {
		t.Fatalf("validate cleanup after concurrent expiry: %v", err)
	}
	assertLegacyCleanupMessage(t, f, row, false, false, false)
}

func TestCleanupMessageLegacyContentAppliesEstimatedByteBudget(t *testing.T) {
	f, subscription, _ := newMessageLegacyCleanupFixture(t)
	largeBody := strings.Repeat("\x01", 180_000)
	var rows []cleanupMessage
	for i := 0; i < 4; i++ {
		rows = append(rows, sendLegacyMessageForCleanup(t, f, fmt.Sprintf("large-%d", i), largeBody, nil))
	}
	applyMessageLegacyCleanupMigration(t, f.db)
	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done, err := messageLegacyCleanupBatch(t.Context(), conn, 8)
	closeErr := conn.Close()
	if err != nil || done || closeErr != nil {
		t.Fatalf("byte-budgeted cleanup batch: done=%t err=%v close=%v", done, err, closeErr)
	}
	var serializedSize int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT octet_length(payload::text) FROM entry
		WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, rows[0].cursor).Scan(&serializedSize); err != nil {
		t.Fatal(err)
	}
	if serializedSize <= 1<<20 {
		t.Fatalf("valid control-character Message JSON size = %d bytes, want over 1 MiB", serializedSize)
	}
	for i, row := range rows {
		var payloadNull bool
		if err := f.db.QueryRowContext(t.Context(), `SELECT payload IS NULL FROM entry WHERE inbox_id = $1 AND cursor = $2`,
			subscription.InboxID, row.cursor).Scan(&payloadNull); err != nil {
			t.Fatal(err)
		}
		if payloadNull != (i == 0) {
			t.Fatalf("cursor %d legacy JSON null = %t after byte-budgeted batch; want %t", row.cursor, payloadNull, i == 0)
		}
	}
	assertCleanupProgress(t, f.db, subscription.InboxID, rows[0].cursor, "cleaning")
}

func TestCleanupMessageLegacyContentCheckpointFencesOldCommandImage(t *testing.T) {
	f, _, _ := newMessageLegacyCleanupFixture(t)
	row := sendLegacyMessageForCleanup(t, f, "fence", "do not lose me", nil)
	applyMessageLegacyCleanupMigration(t, f.db)
	f.exec(t, `DROP TABLE notification_message_legacy_cleanup`)
	if err := CleanupMessageLegacyContent(t.Context(), f.db, 1); err == nil || !strings.Contains(err.Error(), "notification_message_legacy_cleanup") {
		t.Fatalf("cleanup after checkpoint retirement = %v, want first-operation checkpoint error", err)
	}
	assertLegacyCleanupMessage(t, f, row, true, true, false)
}

func TestCleanupMessageLegacyContentUsesNativeKeysetAndSetBasedPlans(t *testing.T) {
	db, ctx := newMessageLegacyCleanupDatabase(t, messageLegacyCleanupMigrationVersion-1)
	f := fixtureForDatabase(t, db, messageQuotas())
	creation := body("cleanup-plan")
	subscription := f.subscribe(t, creation)
	const rowCount = 100_000
	if _, err := db.ExecContext(ctx, `INSERT INTO entry (
		inbox_id, cursor, payload, created_at, identity_provider, message_id,
		message_sender_namespace, message_sender_name, message_body)
		SELECT $1, sequence, jsonb_build_object('body', 'plan body'), now(), 'message',
			md5(sequence::text)::uuid, 'plan-sender-ns', 'plan-sender', 'plan body'
		FROM generate_series(1, $2) AS series(sequence)`, subscription.InboxID, rowCount); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE inbox SET last_cursor = $2 WHERE id = $1`, subscription.InboxID, rowCount)
	applyMessageLegacyCleanupMigration(t, db)
	conn, err := db.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := checkMessageLegacyCleanupPlans(ctx, conn, subscription.InboxID); err != nil {
		t.Fatal(err)
	}
}

func TestCleanupMessageLegacyContentCompletesFreshEmptyDatabase(t *testing.T) {
	db, ctx := newMessageLegacyCleanupDatabase(t, messageLegacyCleanupMigrationVersion)
	if err := CleanupMessageLegacyContent(ctx, db, 16); err != nil {
		t.Fatalf("cleanup fresh empty database: %v", err)
	}
	assertMessageLegacyCleanupPhase(t, db, "cleaned")
	assertMessageLegacyCleanupConstraintsValidated(t, db)
}

type cleanupMessage struct {
	inboxID     uuid.UUID
	cursor      int64
	messageID   uuid.UUID
	sender      ServiceAccount
	body        string
	fromSession *string
	digest      []byte
}

func newMessageLegacyCleanupFixture(t *testing.T) (fixture, Subscription, Subscribe) {
	t.Helper()
	db, _ := newMessageLegacyCleanupDatabase(t, messageLegacyCleanupMigrationVersion-1)
	f := fixtureForDatabase(t, db, messageQuotas())
	creation := body("message-legacy-cleanup")
	subscription := f.subscribe(t, creation)
	return f, subscription, creation
}

func newMessageLegacyCleanupDatabase(t *testing.T, version int64) (*sql.DB, context.Context) {
	t.Helper()
	db, ctx := newStorageIntegrationDatabase(t)
	if _, err := db.ExecContext(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset cloned test database schema: %v", err)
	}
	if err := migrations.UpTo(ctx, db, version); err != nil {
		t.Fatalf("apply Notification migrations through %05d: %v", version, err)
	}
	return db, ctx
}

func applyMessageLegacyCleanupMigration(t *testing.T, db *sql.DB) {
	t.Helper()
	if err := migrations.UpTo(t.Context(), db, messageLegacyCleanupMigrationVersion); err != nil {
		t.Fatalf("apply Message legacy cleanup migration: %v", err)
	}
}

func sendLegacyMessageForCleanup(t *testing.T, f fixture, key, text string, fromSession *string) cleanupMessage {
	t.Helper()
	sender := newOwner()
	message := f.message(sender, key, text)
	message.FromSession = fromSession
	sent := f.send(t, message)
	var inboxID uuid.UUID
	var cursor int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT inbox_id, cursor FROM entry WHERE message_id = $1`, sent.ID).
		Scan(&inboxID, &cursor); err != nil {
		t.Fatalf("find new TEXT-only Message entry: %v", err)
	}
	row := cleanupMessage{inboxID: inboxID, cursor: cursor, messageID: sent.ID, sender: sender, body: text, fromSession: fromSession}
	writeLegacyMessageCleanupPayload(t, f, row, text, fromSession)
	row.digest = cleanupMessageDigest(t, f.db, sent.ID)
	return row
}

func writeLegacyMessageCleanupPayload(t *testing.T, f fixture, row cleanupMessage, text string, fromSession *string) {
	t.Helper()
	message := f.message(row.sender, fmt.Sprintf("cleanup-%s", row.messageID), text)
	message.FromSession = fromSession
	payload, err := message.payload()
	if err != nil {
		t.Fatal(err)
	}
	legacyProto := legacyMessageCleanupProto(text, fromSession)
	if _, err := f.db.ExecContext(t.Context(), `UPDATE entry SET payload = $3::jsonb, typed_payload = $4
		WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor, string(payload), legacyProto); err != nil {
		t.Fatalf("seed historical Message JSON/protobuf: %v", err)
	}
}

func legacyMessageCleanupProto(body string, fromSession *string) []byte {
	var message []byte
	message = appendProtoBytesField(message, 1, []byte(body))
	if fromSession != nil {
		message = appendProtoBytesField(message, 2, []byte(*fromSession))
	}
	return appendProtoBytesField(nil, 3, message)
}

func appendProtoBytesField(dst []byte, field int, value []byte) []byte {
	dst = binary.AppendUvarint(dst, uint64(field<<3|2))
	dst = binary.AppendUvarint(dst, uint64(len(value)))
	return append(dst, value...)
}

func assertLegacyCleanupMessage(t *testing.T, f fixture, row cleanupMessage, legacyPresent, wantBody, fromSessionPresent bool) {
	t.Helper()
	var body, fromSession, payload sql.NullString
	var typedPayload []byte
	var provider, senderNamespace, senderName string
	var messageID uuid.UUID
	if err := f.db.QueryRowContext(t.Context(), `SELECT message_body, message_from_session, payload::text, typed_payload,
		identity_provider, message_id, message_sender_namespace, message_sender_name
		FROM entry WHERE inbox_id = $1 AND cursor = $2`, row.inboxID, row.cursor).
		Scan(&body, &fromSession, &payload, &typedPayload, &provider, &messageID, &senderNamespace, &senderName); err != nil {
		t.Fatal(err)
	}
	if body.Valid != wantBody || (wantBody && body.String != row.body) {
		t.Errorf("cursor %d body = (%t, %q), want present=%t value=%q", row.cursor, body.Valid, body.String, wantBody, row.body)
	}
	if fromSession.Valid != fromSessionPresent || (fromSessionPresent && fromSession.String != *row.fromSession) {
		t.Errorf("cursor %d from_session = (%t, %q), want present=%t value=%v", row.cursor,
			fromSession.Valid, fromSession.String, fromSessionPresent, row.fromSession)
	}
	if payload.Valid != legacyPresent || (legacyPresent && typedPayload == nil) || (!legacyPresent && typedPayload != nil) {
		t.Errorf("cursor %d legacy payload is present=%t and protobuf bytes=%d, want both present=%t", row.cursor,
			payload.Valid, len(typedPayload), legacyPresent)
	}
	if provider != "message" || messageID != row.messageID || senderNamespace != row.sender.Namespace || senderName != row.sender.Name {
		t.Errorf("cursor %d relational Message identity changed: provider=%q id=%s sender=%s/%s", row.cursor,
			provider, messageID, senderNamespace, senderName)
	}
	if digest := cleanupMessageDigest(t, f.db, row.messageID); !bytes.Equal(digest, row.digest) {
		t.Errorf("cursor %d message.digest changed", row.cursor)
	}
}

type cleanupProviderRow struct {
	cursor       int64
	provider     string
	payload      sql.NullString
	typedPayload []byte
}

func readCleanupProviderRows(t *testing.T, db *sql.DB, inboxID uuid.UUID) []cleanupProviderRow {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT cursor, identity_provider, payload::text, typed_payload
		FROM entry WHERE inbox_id = $1 AND identity_provider <> 'message' ORDER BY cursor`, inboxID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var result []cleanupProviderRow
	for rows.Next() {
		var row cleanupProviderRow
		if err := rows.Scan(&row.cursor, &row.provider, &row.payload, &row.typedPayload); err != nil {
			t.Fatal(err)
		}
		result = append(result, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return result
}

func assertCleanupProviderRows(t *testing.T, db *sql.DB, inboxID uuid.UUID, want []cleanupProviderRow) {
	t.Helper()
	got := readCleanupProviderRows(t, db, inboxID)
	if len(got) != len(want) {
		t.Fatalf("non-Message rows = %d, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i].cursor != want[i].cursor || got[i].provider != want[i].provider ||
			got[i].payload != want[i].payload || !bytes.Equal(got[i].typedPayload, want[i].typedPayload) {
			t.Errorf("non-Message row %d changed: got %+v want %+v", i, got[i], want[i])
		}
	}
}

func assertMessageLegacyCleanupConstraintsValidated(t *testing.T, db *sql.DB) {
	t.Helper()
	for _, constraint := range messageLegacyCleanupConstraints {
		var validated bool
		if err := db.QueryRowContext(t.Context(), `SELECT convalidated FROM pg_constraint
			WHERE conrelid = 'entry'::regclass AND conname = $1`, constraint).Scan(&validated); err != nil {
			t.Fatalf("read %s validation state: %v", constraint, err)
		}
		if !validated {
			t.Errorf("constraint %s remains NOT VALID after cleanup", constraint)
		}
	}
}

func assertMessageLegacyCleanupPhase(t *testing.T, db *sql.DB, want string) {
	t.Helper()
	var phase string
	if err := db.QueryRowContext(t.Context(), `SELECT phase FROM notification_message_legacy_cleanup WHERE singleton`).Scan(&phase); err != nil {
		t.Fatal(err)
	}
	if phase != want {
		t.Fatalf("Message legacy cleanup phase = %q, want %q", phase, want)
	}
}

func assertCleanupProgress(t *testing.T, db *sql.DB, inboxID uuid.UUID, cursor int64, wantPhase string) {
	t.Helper()
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	var phase string
	if err := db.QueryRowContext(t.Context(), `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_message_legacy_cleanup WHERE singleton`).Scan(&lastInboxID, &lastCursor, &phase); err != nil {
		t.Fatal(err)
	}
	if !lastInboxID.Valid || lastInboxID.String != inboxID.String() || !lastCursor.Valid || lastCursor.Int64 != cursor || phase != wantPhase {
		t.Fatalf("cleanup progress = (%q, %d, %q), want (%s, %d, %q)",
			lastInboxID.String, lastCursor.Int64, phase, inboxID, cursor, wantPhase)
	}
}

func assertCleanupCursorEmpty(t *testing.T, db *sql.DB) {
	t.Helper()
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	if err := db.QueryRowContext(t.Context(), `SELECT last_inbox_id::text, last_cursor
		FROM notification_message_legacy_cleanup WHERE singleton`).Scan(&lastInboxID, &lastCursor); err != nil {
		t.Fatal(err)
	}
	if lastInboxID.Valid || lastCursor.Valid {
		t.Fatalf("cleanup failure retained stale cursor (%q, %d)", lastInboxID.String, lastCursor.Int64)
	}
}

func cleanupMessageDigest(t *testing.T, db *sql.DB, messageID uuid.UUID) []byte {
	t.Helper()
	var digest []byte
	if err := db.QueryRowContext(t.Context(), `SELECT digest FROM message WHERE id = $1`, messageID).Scan(&digest); err != nil {
		t.Fatal(err)
	}
	return digest
}

func checkMessageLegacyCleanupPlans(ctx context.Context, conn *sql.Conn, inboxID uuid.UUID) error {
	if _, err := conn.ExecContext(ctx, `ANALYZE entry`); err != nil {
		return fmt.Errorf("analyze entry before checking cleanup plans: %w", err)
	}
	var estimatedRows float64
	if err := conn.QueryRowContext(ctx, `SELECT reltuples FROM pg_class WHERE oid = 'entry'::regclass`).Scan(&estimatedRows); err != nil {
		return err
	}
	if estimatedRows < 100_000 {
		return fmt.Errorf("Message cleanup plan fixture has only %.0f estimated rows; want at least 100000", estimatedRows)
	}
	var laterCursor int64
	if err := conn.QueryRowContext(ctx, `SELECT cursor FROM entry WHERE inbox_id = $1 ORDER BY cursor OFFSET 1000 LIMIT 1`, inboxID).Scan(&laterCursor); err != nil {
		return fmt.Errorf("read later Message cleanup cursor: %w", err)
	}
	ids := make([]string, 512)
	cursors := make([]int64, 512)
	for i := range ids {
		ids[i] = inboxID.String()
		cursors[i] = int64(i + 1)
	}
	idsArray, cursorsArray := pq.Array(ids), pq.Array(cursors)
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	plans := []struct {
		name          string
		query         string
		args          []any
		orderedKeyset bool
	}{
		{name: "initial keyset scan", query: messageLegacyCleanupFromStart, args: []any{512}, orderedKeyset: true},
		{name: "later keyset scan", query: messageLegacyCleanupAfter, args: []any{inboxID, laterCursor, 512}, orderedKeyset: true},
		{name: "512-row bulk clear", query: messageLegacyCleanupUpdate, args: []any{idsArray, cursorsArray}},
	}
	for _, check := range plans {
		plan, err := explainLines(ctx, tx, check.query, check.args...)
		if err != nil {
			return fmt.Errorf("explain Message cleanup %s: %w", check.name, err)
		}
		if !strings.Contains(plan, "entry_pkey") || strings.Contains(plan, "Seq Scan on entry") {
			return fmt.Errorf("Message cleanup %s plan must use entry_pkey without an entry table scan:\n%s", check.name, plan)
		}
		if check.orderedKeyset && strings.Contains(plan, "Sort") {
			return fmt.Errorf("Message cleanup %s plan must preserve native primary-key order without a sort:\n%s", check.name, plan)
		}
	}
	return nil
}
