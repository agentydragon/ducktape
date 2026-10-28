package store

import (
	"database/sql"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
)

func TestNativeOnlyWriterMigrationRequiresBackfillAndRetiresUnsafeCommand(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 18); err != nil {
		t.Fatalf("build predecessor schema through migration 00018: %v", err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("typed-only"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	eventJSON, err := marshalHistoricalEventIdentity(event)
	if err != nil {
		t.Fatal(err)
	}
	insertContentBackfilledEntry(t, db, subscription.InboxID, 1, eventJSON, entryPayloadForTest(t, event))

	if err := migrations.UpTo(ctx, db, 19); err == nil || !strings.Contains(err.Error(), "entry content backfill is incomplete") {
		t.Fatalf("writer-only migration error = %v, want the incomplete-backfill gate", err)
	}
	var payloadIsNull bool
	var nativeAt sql.NullTime
	if err := db.QueryRowContext(ctx, `SELECT payload IS NULL, action_at FROM entry WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID).
		Scan(&payloadIsNull, &nativeAt); err != nil {
		t.Fatalf("read Action entry after rejected migration: %v", err)
	}
	if payloadIsNull || !nativeAt.Valid {
		t.Fatalf("historical dual-write row has legacy-null=%t native-at=%v; want JSON and native fields preserved",
			payloadIsNull, nativeAt)
	}

	if _, err := db.ExecContext(ctx, `INSERT INTO notification_entry_content_backfill (singleton, phase)
		VALUES (true, 'backfilled')`); err != nil {
		t.Fatalf("mark the completed offline backfill: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 19); err != nil {
		t.Fatalf("apply writer-only migration after backfill completion: %v", err)
	}
	var checkpointExists bool
	if err := db.QueryRowContext(ctx, `SELECT to_regclass('notification_entry_content_backfill') IS NOT NULL`).Scan(&checkpointExists); err != nil {
		t.Fatal(err)
	}
	if checkpointExists {
		t.Fatal("writer-only migration retained the unsafe content-backfill checkpoint")
	}

	// The dual-write image's command first inserts or locks this row. It must fail before any batch can clear
	// retained content after the checkpoint has been retired.
	if _, err := db.ExecContext(ctx, `INSERT INTO notification_entry_content_backfill (singleton)
		VALUES (true) ON CONFLICT (singleton) DO NOTHING`); err == nil {
		t.Fatal("old content-backfill checkpoint access succeeded after writer-only cutover")
	}
	if err := db.QueryRowContext(ctx, `SELECT payload IS NULL, action_at FROM entry WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID).
		Scan(&payloadIsNull, &nativeAt); err != nil {
		t.Fatalf("read live Action entry after rejected old command access: %v", err)
	}
	if payloadIsNull || !nativeAt.Valid {
		t.Fatalf("old command access changed retained content: legacy-null=%t native-at=%v", payloadIsNull, nativeAt)
	}
}

func TestNativeOnlyWriterMigrationAllowsFreshEmptyDatabase(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 18); err != nil {
		t.Fatalf("build predecessor schema through migration 00018: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 19); err != nil {
		t.Fatalf("fresh empty database should not need a content-backfill marker: %v", err)
	}
}

func resetNotificationSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset cloned test database schema: %v", err)
	}
}
