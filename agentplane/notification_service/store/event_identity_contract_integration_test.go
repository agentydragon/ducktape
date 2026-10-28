package store

import (
	"database/sql"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
)

func TestEventIdentityContractRequiresOfflineProofAndKeepsRelationalDedup(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 21); err != nil {
		t.Fatalf("build schema through legacy cleanup migration 00021: %v", err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("event-identity-contract"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))

	if err := migrations.UpTo(ctx, db, 22); err == nil || !strings.Contains(err.Error(), "requires completed backfill") {
		t.Fatalf("contract migration error = %v, want populated-database offline proof gate", err)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_attribute
		WHERE attrelid = 'entry'::regclass AND attname = 'event' AND NOT attisdropped`); got != 1 {
		t.Fatalf("event column after rejected contract = %d active attributes, want 1", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1 AND event IS NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("native-only row after rejected contract = %d, want 1", got)
	}

	recordEventIdentityWriterOnlyProof(t, db)
	if err := migrations.UpTo(ctx, db, 22); err != nil {
		t.Fatalf("apply identity contract after durable offline proof: %v", err)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_attribute
		WHERE attrelid = 'entry'::regclass AND attname = 'event' AND NOT attisdropped`); got != 0 {
		t.Fatalf("event column after contract = %d active attributes, want none", got)
	}
	var providerRequired bool
	if err := db.QueryRowContext(ctx, `SELECT attnotnull FROM pg_attribute
		WHERE attrelid = 'entry'::regclass AND attname = 'identity_provider' AND NOT attisdropped`).Scan(&providerRequired); err != nil {
		t.Fatal(err)
	}
	if !providerRequired {
		t.Fatal("identity_provider remains nullable after the contract")
	}
	if got := f.count(t, `SELECT count(*) FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname IN (
			'entry_identity_provider_required', 'entry_message_body_ceiling', 'entry_inbox_id_event_key')`); got != 0 {
		t.Fatalf("legacy identity constraints after contract = %d, want none", got)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_class WHERE relname = 'notification_event_identity_backfill'`); got != 0 {
		t.Fatalf("identity backfill checkpoint tables after contract = %d, want none", got)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_index i
		JOIN pg_class c ON c.oid = i.indexrelid
		WHERE i.indrelid = 'entry'::regclass AND c.relname LIKE 'entry_identity_%_key'
		AND i.indisvalid AND i.indisready AND i.indisunique`); got != 6 {
		t.Fatalf("natural-key indexes after contract = %d, want 6", got)
	}

	page := f.read(t, subscription.InboxID, 0)
	if len(page.Entries) != 1 || page.Entries[0].Event != event {
		t.Fatalf("read after identity contract = %+v, want retained event %+v", page.Entries, event)
	}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	if got := f.read(t, subscription.InboxID, 0).Inbox.LastCursor; got != 1 {
		t.Fatalf("last cursor after replaying contracted identity = %d, want 1", got)
	}
}

func recordEventIdentityWriterOnlyProof(t *testing.T, db *sql.DB) {
	t.Helper()
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(t.Context(), `ALTER TABLE entry
		VALIDATE CONSTRAINT entry_identity_shape,
		VALIDATE CONSTRAINT entry_identity_provider_required,
		VALIDATE CONSTRAINT entry_message_body_bytes`); err != nil {
		t.Fatalf("record offline validation proofs: %v", err)
	}
	result, err := tx.ExecContext(t.Context(), `UPDATE notification_event_identity_backfill
		SET phase = 'writer_only' WHERE singleton AND phase = 'indexed'`)
	if err != nil {
		t.Fatalf("record writer-only phase: %v", err)
	}
	updated, err := result.RowsAffected()
	if err != nil {
		t.Fatal(err)
	}
	if updated != 1 {
		t.Fatalf("writer-only phase rows updated = %d, want 1", updated)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit offline proof: %v", err)
	}
}

func TestEventIdentityContractAllowsFreshEmptyDatabaseWithoutOfflineCommand(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 22); err != nil {
		t.Fatalf("apply migrations through identity contract migration 00022 on a fresh database: %v", err)
	}
	var eventExists, providerRequired, checkpointExists bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM pg_attribute WHERE attrelid = 'entry'::regclass AND attname = 'event' AND NOT attisdropped
	), (SELECT attnotnull FROM pg_attribute
		WHERE attrelid = 'entry'::regclass AND attname = 'identity_provider' AND NOT attisdropped),
		(to_regclass('notification_event_identity_backfill') IS NOT NULL)`).
		Scan(&eventExists, &providerRequired, &checkpointExists); err != nil {
		t.Fatal(err)
	}
	if eventExists || !providerRequired || checkpointExists {
		t.Fatalf("fresh contract state: event exists=%t provider required=%t checkpoint exists=%t", eventExists, providerRequired, checkpointExists)
	}
	var validated bool
	if err := db.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname = 'entry_message_body_bytes'`).Scan(&validated); err != nil {
		t.Fatal(err)
	}
	if !validated {
		t.Fatal("fresh contract did not validate and retain the relational Message body ceiling")
	}
}
