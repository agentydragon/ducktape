package migrations

import (
	"context"
	"database/sql"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/google/uuid"
	"github.com/pressly/goose/v3"
)

// applyUpTo applies every migration whose name sorts before the exclusive bound, so a test can
// exercise one migration against the schema its predecessor left.
func applyUpTo(ctx context.Context, t *testing.T, db *sql.DB, exclusiveBound string) {
	t.Helper()
	provider, err := goose.NewProvider(goose.DialectPostgres, db, previousMigrations(t, exclusiveBound),
		goose.WithTableName("notification_service_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		t.Fatal("create migration provider:", err)
	}
	if _, err := provider.Up(ctx); err != nil {
		t.Fatalf("apply migrations before %s: %v", exclusiveBound, err)
	}
}

// previousMigrations are the migration files whose names sort below the exclusive bound, so a provider
// can stop at the schema one migration left behind.
func previousMigrations(t *testing.T, exclusiveBound string) fstest.MapFS {
	t.Helper()
	files, err := scripts.ReadDir("notifications")
	if err != nil {
		t.Fatal("read notification migrations:", err)
	}
	previous := fstest.MapFS{}
	for _, file := range files {
		if file.Name() >= exclusiveBound {
			continue
		}
		data, err := scripts.ReadFile("notifications/" + file.Name())
		if err != nil {
			t.Fatal("read notification migration", file.Name(), ":", err)
		}
		previous[file.Name()] = &fstest.MapFile{Data: data}
	}
	return previous
}

func prepareSessionReaderSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	resetSchema(t, db)
	applyUpTo(t.Context(), t, db, "00021_")
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
	if _, err := db.ExecContext(t.Context(), `UPDATE inbox SET owner_namespace = 'sandbox-namespace', owner_name = 'sandbox-name',
		destination_key = 'session-key-migration-legacy-sha',
		destination_namespace = 'sandbox-namespace', destination_name = 'sandbox-name', destination_uid = 'sandbox-uid'
		WHERE id = $1`, subscription.InboxID); err != nil {
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

	var sessionID, legacyKey string
	if err := db.QueryRowContext(t.Context(), `SELECT session_id, destination_key FROM inbox WHERE id = $1`, subscription.InboxID).
		Scan(&sessionID, &legacyKey); err != nil {
		t.Fatal("read legacy inbox key:", err)
	}
	if legacyKey == sessionID {
		t.Fatal("fixture destination key unexpectedly equals its Session ID")
	}

	applyUpTo(t.Context(), t, db, "00022_")
	assertApplied(t, db, 21)

	var migratedSessionID string
	var lastCursor, acknowledged, covered int64
	if err := db.QueryRowContext(t.Context(), `SELECT session_id, last_cursor, acknowledged, covered
		FROM inbox WHERE id = $1`, subscription.InboxID).Scan(
		&migratedSessionID, &lastCursor, &acknowledged, &covered,
	); err != nil {
		t.Fatal("read migrated inbox:", err)
	}
	if migratedSessionID != sessionID {
		t.Fatalf("migrated session = %q, want %q", migratedSessionID, sessionID)
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
	// The reader schema is recorded through version 20, so the service's own migrator runs 21 and
	// then 22; 21 refuses the unkeyed row and neither advances.
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
	for _, version := range []int64{21, 22} {
		var applied int
		if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM notification_service_goose_version WHERE version_id = $1 AND is_applied`, version).Scan(&applied); err != nil {
			t.Fatal("inspect failed migration history:", err)
		}
		if applied != 0 {
			t.Fatalf("migration %d recorded %d applied rows after refusing an unkeyed inbox", version, applied)
		}
	}
}

// TestContractMigrationRekeysAWriterThatOutranTheCutover covers the shape found in staging: a writer
// predating the cutover derived a row's key from its Sandbox after migration 21 had keyed it by Session.
// The contract step rekeys that row instead of refusing, because the key is derived from the Sandbox the
// Session already names and one inbox per Session is already enforced, and it keeps the mailbox state.
func TestContractMigrationRekeysAWriterThatOutranTheCutover(t *testing.T) {
	db := cloneDatabase(t)
	resetSchema(t, db)
	applyUpTo(t.Context(), t, db, "00022_")
	assertApplied(t, db, 21)
	_, subscription := seedSubscription(t, db, "contract-migration")
	if _, err := db.ExecContext(t.Context(), `UPDATE inbox SET destination_key = 'pre-cutover-derived-key',
		owner_namespace = 'agentplane', owner_name = 'pre-cutover-worker',
		destination_namespace = 'agentplane', destination_name = 'pre-cutover-worker', destination_uid = 'pre-cutover-sandbox-uid'
		WHERE id = $1`, subscription.InboxID); err != nil {
		t.Fatal("restore a pre-cutover writer's inbox fixture:", err)
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE inbox SET last_cursor = 1, covered = 1 WHERE id = $1`, subscription.InboxID); err != nil {
		t.Fatal("set the retained unread cursor:", err)
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatal("contract migration over a pre-cutover writer's key:", err)
	}
	assertApplied(t, db, 22)

	var sessionID string
	var lastCursor, acknowledged, covered int64
	if err := db.QueryRowContext(t.Context(), `SELECT session_id, last_cursor, acknowledged, covered
		FROM inbox WHERE id = $1`, subscription.InboxID).Scan(
		&sessionID, &lastCursor, &acknowledged, &covered,
	); err != nil {
		t.Fatal("read the rekeyed inbox:", err)
	}
	if sessionID != "contract-migration" {
		t.Fatalf("rekeyed session = %q, want contract-migration", sessionID)
	}
	if lastCursor != 1 || acknowledged != 0 || covered != 1 {
		t.Fatalf("rekeyed mailbox cursors = last %d, acknowledged %d, covered %d; want 1, 0, 1", lastCursor, acknowledged, covered)
	}
	var subscriptions int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM subscription WHERE inbox_id = $1`, subscription.InboxID).Scan(&subscriptions); err != nil {
		t.Fatal("count the retained subscription:", err)
	}
	if subscriptions != 1 {
		t.Fatalf("subscriptions after the contract rekey = %d, want 1", subscriptions)
	}
	for _, column := range []string{"destination_key", "destination_namespace", "destination_name", "destination_uid", "owner_namespace", "owner_name"} {
		var present bool
		if err := db.QueryRowContext(t.Context(), `SELECT EXISTS (
			SELECT 1 FROM pg_attribute WHERE attrelid = 'inbox'::regclass AND attname = $1 AND NOT attisdropped)`, column).Scan(&present); err != nil {
			t.Fatalf("inspect inbox.%s: %v", column, err)
		}
		if present {
			t.Errorf("inbox.%s survived the Session key contract", column)
		}
	}
}
