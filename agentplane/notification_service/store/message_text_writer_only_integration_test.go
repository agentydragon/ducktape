package store

import (
	"bytes"
	"context"
	"database/sql"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
)

func TestMessageTextWriterMigrationRequiresBackfillAndRetiresOldCommand(t *testing.T) {
	db, ctx := newMessageTextWriterMigrationDatabase(t)
	f := fixtureForDatabase(t, db, messageQuotas())
	f.openInbox(t)
	sender := newOwner()
	fromSession := "launch-session"
	message := f.message(sender, "writer-only", "writer body")
	message.FromSession = &fromSession
	sent := f.send(t, message)

	legacyPayload, err := encodeMessagePayload(message.Body, message.FromSession)
	if err != nil {
		t.Fatal(err)
	}
	// This is a literal EntryContent.Message wire value from the dual-write release. Keep the test independent of
	// the production protobuf codec, which the writer-only release removes.
	legacyTypedPayload := []byte("\x1a\x1d\x0a\x0bwriter body\x12\x0elaunch-session")
	if _, err := db.ExecContext(ctx, `UPDATE entry SET payload = $2::jsonb, typed_payload = $3 WHERE message_id = $1`,
		sent.ID, string(legacyPayload), legacyTypedPayload); err != nil {
		t.Fatalf("seed retained dual-write representations: %v", err)
	}
	before := readMessageWriterSnapshot(t, db, sent.ID)
	if !before.payload.Valid || !bytes.Equal(before.typedPayload, legacyTypedPayload) ||
		!before.messageBody.Valid || before.messageBody.String != message.Body ||
		!before.messageFromSession.Valid || before.messageFromSession.String != fromSession {
		t.Fatalf("seeded Message representations = %+v; want legacy JSON/protobuf and canonical text", before)
	}

	if err := migrations.UpTo(ctx, db, 23); err == nil || !strings.Contains(err.Error(), "Message text backfill is incomplete") {
		t.Fatalf("text-only writer migration error = %v, want incomplete-backfill gate", err)
	}
	assertLatestGooseVersion(t, db, 21)
	assertTableExists(t, db, "notification_message_text_backfill", true)
	if after := readMessageWriterSnapshot(t, db, sent.ID); !sameMessageWriterSnapshot(before, after) {
		t.Fatalf("rejected migration changed retained Message: before %+v, after %+v", before, after)
	}

	if _, err := db.ExecContext(ctx, `UPDATE notification_message_text_backfill SET phase = 'backfilled' WHERE singleton`); err != nil {
		t.Fatalf("mark completed offline backfill: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 23); err != nil {
		t.Fatalf("apply text-only writer migration after completed backfill: %v", err)
	}
	assertTableExists(t, db, "notification_message_text_backfill", false)
	if retry := f.send(t, message); retry.ID != sent.ID {
		t.Fatalf("retry after text-only cutover = %+v, want original receipt %s", retry, sent.ID)
	}
	if after := readMessageWriterSnapshot(t, db, sent.ID); !sameMessageWriterSnapshot(before, after) {
		t.Fatalf("idempotent retry changed retained Message: before %+v, after %+v", before, after)
	}

	// The old command checks the checkpoint before entries. With the table gone it must fail before it can inspect
	// or clear retained legacy representations.
	if err := db.QueryRowContext(ctx, `SELECT phase FROM notification_message_text_backfill WHERE singleton`).Scan(new(string)); err == nil || !strings.Contains(err.Error(), "notification_message_text_backfill") {
		t.Fatalf("stale text backfill first operation error = %v, want checkpoint-table failure", err)
	}
	if after := readMessageWriterSnapshot(t, db, sent.ID); !sameMessageWriterSnapshot(before, after) {
		t.Fatalf("retired command changed retained Message: before %+v, after %+v", before, after)
	}
}

func TestMessageTextWriterMigrationAllowsFreshEmptyDatabase(t *testing.T) {
	db, ctx := newMessageTextWriterMigrationDatabase(t)
	var entries int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM entry`).Scan(&entries); err != nil {
		t.Fatal(err)
	}
	if entries != 0 {
		t.Fatalf("fresh predecessor database has %d entries, want none", entries)
	}
	if err := migrations.UpTo(ctx, db, 23); err != nil {
		t.Fatalf("empty database should not need a Message text backfill marker: %v", err)
	}
	assertTableExists(t, db, "notification_message_text_backfill", false)
}

func newMessageTextWriterMigrationDatabase(t *testing.T) (*sql.DB, context.Context) {
	t.Helper()
	db, ctx := newStorageIntegrationDatabase(t)
	if _, err := db.ExecContext(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset cloned database schema: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 22); err != nil {
		t.Fatalf("apply schema through predecessor migration 00022: %v", err)
	}
	return db, ctx
}

type messageWriterSnapshot struct {
	inboxID            string
	cursor             int64
	event              sql.NullString
	provider           string
	messageID          string
	senderNamespace    string
	senderName         string
	payload            sql.NullString
	typedPayload       []byte
	messageBody        sql.NullString
	messageFromSession sql.NullString
	digest             []byte
}

func readMessageWriterSnapshot(t *testing.T, db *sql.DB, messageID uuid.UUID) messageWriterSnapshot {
	t.Helper()
	var snapshot messageWriterSnapshot
	err := db.QueryRowContext(t.Context(), `SELECT e.inbox_id::text, e.cursor, e.event::text, e.identity_provider,
		e.message_id::text, e.message_sender_namespace, e.message_sender_name,
		e.payload::text, e.typed_payload, e.message_body, e.message_from_session, m.digest
		FROM entry e JOIN message m ON m.inbox_id = e.inbox_id AND m.cursor = e.cursor
		WHERE e.message_id = $1`, messageID).Scan(
		&snapshot.inboxID, &snapshot.cursor, &snapshot.event, &snapshot.provider,
		&snapshot.messageID, &snapshot.senderNamespace, &snapshot.senderName,
		&snapshot.payload, &snapshot.typedPayload, &snapshot.messageBody, &snapshot.messageFromSession, &snapshot.digest,
	)
	if err != nil {
		t.Fatalf("read Message writer snapshot: %v", err)
	}
	return snapshot
}

func sameMessageWriterSnapshot(a, b messageWriterSnapshot) bool {
	return a.inboxID == b.inboxID && a.cursor == b.cursor && a.event == b.event && a.provider == b.provider &&
		a.messageID == b.messageID && a.senderNamespace == b.senderNamespace && a.senderName == b.senderName &&
		a.payload == b.payload && bytes.Equal(a.typedPayload, b.typedPayload) &&
		a.messageBody == b.messageBody && a.messageFromSession == b.messageFromSession && bytes.Equal(a.digest, b.digest)
}
