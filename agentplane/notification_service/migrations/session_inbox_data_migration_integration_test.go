package migrations

import (
	"database/sql"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
	"github.com/pressly/goose/v3"
)

func prepareSessionReaderSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	resetSchema(t, db)

	files, err := scripts.ReadDir("notifications")
	if err != nil {
		t.Fatal("read notification migrations:", err)
	}
	previous := fstest.MapFS{}
	for _, file := range files {
		if file.Name() >= "00021_" {
			continue
		}
		data, err := scripts.ReadFile("notifications/" + file.Name())
		if err != nil {
			t.Fatal("read notification migration", file.Name(), ":", err)
		}
		previous[file.Name()] = &fstest.MapFile{Data: data}
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, previous,
		goose.WithTableName("notification_service_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		t.Fatal("create pre-migration provider:", err)
	}
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatal("apply schema through Session reader expansion:", err)
	}
	assertApplied(t, db, 20)
}

func insertUnkeyedInbox(t *testing.T, db *sql.DB, id uuid.UUID) {
	t.Helper()
	_, err := db.ExecContext(t.Context(), `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, session_id,
		last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until,
		destination_namespace, destination_name, destination_uid, owner_identity_name, owner_identity_uid)
		VALUES ($1, 'legacy-namespace', 'legacy-account', 'legacy-key', '', 0, 0, 0, 0,
		false, $2, $2, $2, 'sandbox-namespace', 'sandbox-name', 'sandbox-uid', 'identity', 'identity-uid')`,
		id, time.Now().UTC())
	if err != nil {
		t.Fatal("insert unkeyed legacy inbox:", err)
	}
}

func TestSessionInboxDataMigrationRekeysWithoutChangingMailboxState(t *testing.T) {
	db := cloneDatabase(t)
	prepareSessionReaderSchema(t, db)
	_, subscription := seedSubscription(t, db, "session-key-migration")
	legacyDestination := model.DestinationRef{Namespace: "sandbox-namespace", Name: "sandbox-name", UID: "sandbox-uid"}
	legacyKey := model.DestinationKey(legacyDestination, "session-key-migration")
	if _, err := db.ExecContext(t.Context(), `UPDATE inbox SET owner_namespace = $2, owner_name = $3,
		destination_key = $4, destination_namespace = $2, destination_name = $3, destination_uid = $5
		WHERE id = $1`, subscription.InboxID, legacyDestination.Namespace, legacyDestination.Name, legacyKey, legacyDestination.UID); err != nil {
		t.Fatal("restore legacy Sandbox-keyed inbox fixture:", err)
	}

	entryRequestID := uuid.New()
	if _, err := db.ExecContext(t.Context(), `UPDATE inbox SET last_cursor = 1, covered = 1 WHERE id = $1`, subscription.InboxID); err != nil {
		t.Fatal("set unread inbox cursor:", err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO entry (
		inbox_id, cursor, created_at, identity_provider, actions_request_id, actions_sequence, action_state, action_at)
		VALUES ($1, 1, now(), 'actions', $2, 41, 2, now())`, subscription.InboxID, entryRequestID); err != nil {
		t.Fatal("insert unread entry:", err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO subscription_match (subscription_id, cursor) VALUES ($1, 1)`, subscription.ID); err != nil {
		t.Fatal("insert subscription match:", err)
	}
	commandID := uuid.New()
	runnerEntry := []byte{0x0a, 0x03, 0x72, 0x65, 0x63}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO notice (
		inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
		VALUES ($1, $2, 1, 'retained notice', true, true, true, 73, $3)`,
		subscription.InboxID, commandID, runnerEntry); err != nil {
		t.Fatal("insert delivery receipt:", err)
	}

	var oldKey, sessionID, ownerNamespace, ownerName, destinationNamespace, destinationName, destinationUID string
	if err := db.QueryRowContext(t.Context(), `SELECT destination_key, session_id, owner_namespace, owner_name,
		destination_namespace, destination_name, destination_uid FROM inbox WHERE id = $1`, subscription.InboxID).
		Scan(&oldKey, &sessionID, &ownerNamespace, &ownerName, &destinationNamespace, &destinationName, &destinationUID); err != nil {
		t.Fatal("read legacy inbox key:", err)
	}
	if oldKey == sessionID {
		t.Fatal("fixture destination key unexpectedly equals its Session ID")
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatal("apply Session inbox data migration:", err)
	}

	var newKey, migratedSessionID string
	var ownerNamespaceAfter, ownerNameAfter, destinationNamespaceAfter, destinationNameAfter, destinationUIDAfter string
	var lastCursor, acknowledged, covered int64
	if err := db.QueryRowContext(t.Context(), `SELECT destination_key, session_id, owner_namespace, owner_name,
		destination_namespace, destination_name, destination_uid, last_cursor, acknowledged, covered
		FROM inbox WHERE id = $1`, subscription.InboxID).Scan(
		&newKey, &migratedSessionID, &ownerNamespaceAfter, &ownerNameAfter,
		&destinationNamespaceAfter, &destinationNameAfter, &destinationUIDAfter,
		&lastCursor, &acknowledged, &covered,
	); err != nil {
		t.Fatal("read migrated inbox:", err)
	}
	if newKey != sessionID || migratedSessionID != sessionID {
		t.Fatalf("migrated key/session = %q/%q, want %q", newKey, migratedSessionID, sessionID)
	}
	if ownerNamespaceAfter != ownerNamespace || ownerNameAfter != ownerName ||
		destinationNamespaceAfter != destinationNamespace || destinationNameAfter != destinationName || destinationUIDAfter != destinationUID {
		t.Fatalf("migration changed legacy metadata: owner %q/%q (want %q/%q), destination %q/%q/%q (want %q/%q/%q)",
			ownerNamespaceAfter, ownerNameAfter, ownerNamespace, ownerName,
			destinationNamespaceAfter, destinationNameAfter, destinationUIDAfter,
			destinationNamespace, destinationName, destinationUID)
	}
	if lastCursor != 1 || acknowledged != 0 || covered != 1 {
		t.Fatalf("migrated inbox cursors = last %d, acknowledged %d, covered %d; want 1, 0, 1", lastCursor, acknowledged, covered)
	}

	var gotSubscription, gotEntry, gotMatch int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM subscription WHERE id = $1 AND inbox_id = $2`,
		subscription.ID, subscription.InboxID).Scan(&gotSubscription); err != nil {
		t.Fatal("count retained subscription:", err)
	}
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1
		AND identity_provider = 'actions' AND actions_request_id = $2 AND actions_sequence = 41 AND action_state = 2 AND action_at IS NOT NULL`,
		subscription.InboxID, entryRequestID).Scan(&gotEntry); err != nil {
		t.Fatal("count retained unread entry:", err)
	}
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM subscription_match WHERE subscription_id = $1 AND cursor = 1`,
		subscription.ID).Scan(&gotMatch); err != nil {
		t.Fatal("count retained subscription match:", err)
	}
	var gotCommandID uuid.UUID
	var throughCursor, runnerCursor int64
	var text string
	var attempted, admitted, confirmed bool
	var gotRunnerEntry []byte
	if err := db.QueryRowContext(t.Context(), `SELECT command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry
		FROM notice WHERE inbox_id = $1`, subscription.InboxID).Scan(
		&gotCommandID, &throughCursor, &text, &attempted, &admitted, &confirmed, &runnerCursor, &gotRunnerEntry,
	); err != nil {
		t.Fatal("read retained delivery receipt:", err)
	}
	if gotSubscription != 1 || gotEntry != 1 || gotMatch != 1 {
		t.Fatalf("mailbox rows after rekey: subscription=%d entry=%d match=%d; want 1 each", gotSubscription, gotEntry, gotMatch)
	}
	if gotCommandID != commandID || throughCursor != 1 || text != "retained notice" || !attempted || !admitted || !confirmed ||
		runnerCursor != 73 || string(gotRunnerEntry) != string(runnerEntry) {
		t.Fatalf("delivery receipt changed: command=%s through=%d text=%q attempted=%t admitted=%t confirmed=%t runner_cursor=%d runner_entry=%x",
			gotCommandID, throughCursor, text, attempted, admitted, confirmed, runnerCursor, gotRunnerEntry)
	}
	assertApplied(t, db, 21)
}

func TestSessionInboxDataMigrationReportsUnkeyedRowsAndChangesNothing(t *testing.T) {
	db := cloneDatabase(t)
	prepareSessionReaderSchema(t, db)
	_, validSubscription := seedSubscription(t, db, "session-key-migration-valid")
	invalidID := uuid.New()
	insertUnkeyedInbox(t, db, invalidID)

	var validKeyBefore string
	if err := db.QueryRowContext(t.Context(), `SELECT destination_key FROM inbox WHERE id = $1`, validSubscription.InboxID).Scan(&validKeyBefore); err != nil {
		t.Fatal("read valid key before migration:", err)
	}
	err := Up(t.Context(), db)
	if err == nil || !strings.Contains(err.Error(), invalidID.String()) || !strings.Contains(err.Error(), "no derivable Session ID") {
		t.Fatalf("migration error = %v, want the unkeyed inbox ID and no-derivable-Session report", err)
	}

	var validKeyAfter, invalidKeyAfter, invalidSessionAfter string
	var invalidDestinationUID string
	if err := db.QueryRowContext(t.Context(), `SELECT destination_key FROM inbox WHERE id = $1`, validSubscription.InboxID).Scan(&validKeyAfter); err != nil {
		t.Fatal("read valid key after rejected migration:", err)
	}
	if err := db.QueryRowContext(t.Context(), `SELECT destination_key, session_id, destination_uid FROM inbox WHERE id = $1`, invalidID).
		Scan(&invalidKeyAfter, &invalidSessionAfter, &invalidDestinationUID); err != nil {
		t.Fatal("read unkeyed row after rejected migration:", err)
	}
	if validKeyAfter != validKeyBefore || invalidKeyAfter != "legacy-key" || invalidSessionAfter != "" || invalidDestinationUID != "sandbox-uid" {
		t.Fatalf("failed migration changed inbox data: valid key %q (before %q), invalid key/session/destination %q/%q/%q",
			validKeyAfter, validKeyBefore, invalidKeyAfter, invalidSessionAfter, invalidDestinationUID)
	}
	var applied int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM notification_service_goose_version WHERE version_id = 21 AND is_applied`).Scan(&applied); err != nil {
		t.Fatal("inspect failed migration history:", err)
	}
	if applied != 0 {
		t.Fatalf("migration 21 recorded %d applied rows after refusing an unkeyed inbox", applied)
	}
}
