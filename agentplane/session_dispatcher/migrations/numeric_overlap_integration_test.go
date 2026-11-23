package migrations

import (
	"bytes"
	"database/sql"
	"errors"
	"strings"
	"testing"

	"github.com/lib/pq"
)

// A nonempty database upgrades through an expansion image and the operator's
// backfill before the numeric-only application's migration gate can pass.
func upWithEnumBackfill(t *testing.T, db *sql.DB) error {
	t.Helper()
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 31); err != nil {
		t.Fatal("apply enum expansion:", err)
	}
	if _, err := db.ExecContext(t.Context(), "CALL backfill_dispatch_enum_values(2)"); err != nil {
		t.Fatal("backfill enums:", err)
	}
	return Up(t.Context(), db)
}

func TestDispatcherNumericWriterOverlap(t *testing.T) {
	db := openMigrationDatabase(t)
	ctx := t.Context()
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(ctx, 30); err != nil {
		t.Fatal(err)
	}

	exec := func(query string, args ...any) {
		t.Helper()
		if _, err := db.ExecContext(ctx, query, args...); err != nil {
			t.Fatal(err)
		}
	}
	expectConflict := func(query string) {
		t.Helper()
		_, err := db.ExecContext(ctx, query)
		var pgErr *pq.Error
		if !errors.As(err, &pgErr) || pgErr.Code != "23514" {
			t.Fatalf("want check violation from %s, got %v", query, err)
		}
	}

	const uid = "00000000-0000-4000-8000-000000000032"
	const oldInsert = `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at)
		VALUES ($1,'mixed-session','mixed-environment',$2,$3,decode('0801','hex'),$4,now(),now())`
	exec(oldInsert, "known-legacy", uid, "submit_input", "UNKNOWN")
	exec(oldInsert, "unknown-legacy", uid, "stop_runner_session", "older-state")
	exec(oldInsert, "before-expansion-delete", uid, "interrupt_turn", "FAILED")
	exec("DELETE FROM dispatch WHERE command_id='before-expansion-delete'")
	insertRetainedEvidence(t, db)

	if _, err := provider.UpTo(ctx, 31); err != nil {
		t.Fatal(err)
	}
	if err := Up(ctx, db); err == nil || !strings.Contains(err.Error(), "numeric backfill is incomplete") {
		t.Fatalf("unbackfilled database migration = %v", err)
	}
	if err := upWithEnumBackfill(t, db); err != nil {
		t.Fatal(err)
	}

	type receipt struct {
		textOperation, textState string
		operation, state         sql.NullInt32
	}
	number := func(value int32) sql.NullInt32 { return sql.NullInt32{Int32: value, Valid: true} }
	getReceipt := func(commandID string, deleted bool, want receipt) int64 {
		t.Helper()
		table := "dispatch"
		if deleted {
			table = "dispatch_tombstone"
		}
		var got receipt
		var version int64
		if err := db.QueryRowContext(ctx, `SELECT operation,state,operation_value,state_value,version FROM `+table+`
			WHERE command_id=$1`, commandID).Scan(&got.textOperation, &got.textState, &got.operation, &got.state, &version); err != nil {
			t.Fatal(err)
		}
		if got != want {
			t.Fatalf("%s %s = %+v, want %+v", table, commandID, got, want)
		}
		return version
	}
	getReceipt("known-legacy", false, receipt{"submit_input", "UNKNOWN", number(1), number(8)})
	getReceipt("before-expansion-delete", true, receipt{"interrupt_turn", "FAILED", number(5), number(6)})
	unmapped := receipt{"stop_runner_session", "older-state", sql.NullInt32{}, sql.NullInt32{}}
	getReceipt("unknown-legacy", false, unmapped)
	exec("UPDATE dispatch SET reason='metadata-only' WHERE command_id='unknown-legacy'")
	unmappedVersion := getReceipt("unknown-legacy", false, unmapped)
	exec("DELETE FROM dispatch WHERE command_id='unknown-legacy'")
	if got := getReceipt("unknown-legacy", true, unmapped); got <= unmappedVersion {
		t.Fatalf("unmapped deletion receipt version = %d, prior = %d", got, unmappedVersion)
	}

	const numericInsert = `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation_value,command,state_value,created_at,updated_at)
		VALUES ('numeric-writer','mixed-session','mixed-environment',$1,5,decode('0801','hex'),1,now(),now())`
	exec(numericInsert, uid)
	queued := receipt{"interrupt_turn", "QUEUED_IN_DISPATCHER", number(5), number(1)}
	before := getReceipt("numeric-writer", false, queued)

	expectConflict(`UPDATE dispatch SET state='FAILED',state_value=2 WHERE command_id='numeric-writer'`)
	expectConflict(`UPDATE dispatch SET operation='change_model',operation_value=3 WHERE command_id='numeric-writer'`)
	expectConflict(`UPDATE dispatch SET operation_value=6 WHERE command_id='numeric-writer'`)
	expectConflict(`UPDATE dispatch SET state_value=NULL WHERE command_id='numeric-writer'`)
	if got := getReceipt("numeric-writer", false, queued); got != before {
		t.Fatalf("rejected writes changed version from %d to %d", before, got)
	}

	exec(`UPDATE dispatch SET state_value=2 WHERE command_id='numeric-writer'`)
	delivering := getReceipt("numeric-writer", false, receipt{"interrupt_turn", "DELIVERING_TO_RUNNER", number(5), number(2)})
	if delivering <= before {
		t.Fatalf("numeric update did not advance version: %d to %d", before, delivering)
	}
	// A rollback image can still initialize and update rows created by a
	// numeric-only replica. Its SQL never mentions the shadow columns.
	legacySchema := schema
	legacySchema.Files = migrationFilesThrough(t, 30)
	legacy, err := legacySchema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Up(ctx); err != nil {
		t.Fatalf("legacy image rollback startup: %v", err)
	}
	exec(`UPDATE dispatch SET state='FAILED' WHERE command_id='numeric-writer'`)
	failed := receipt{"interrupt_turn", "FAILED", number(5), number(6)}
	failedVersion := getReceipt("numeric-writer", false, failed)
	if failedVersion <= delivering {
		t.Fatalf("legacy update did not advance version: %d to %d", delivering, failedVersion)
	}
	exec(`UPDATE dispatch SET state='UNKNOWN',state_value=8 WHERE command_id='numeric-writer'`)
	explicitUnknown := receipt{"interrupt_turn", "UNKNOWN", number(5), number(8)}
	latest := getReceipt("numeric-writer", false, explicitUnknown)
	exec(`DELETE FROM dispatch WHERE command_id='numeric-writer'`)
	if got := getReceipt("numeric-writer", true, explicitUnknown); got <= latest {
		t.Fatalf("numeric deletion receipt version = %d, prior = %d", got, latest)
	}

	// The older Environment cleanup must still retain its exact Session's
	// command. Test the original BEFORE DELETE fence alongside tombstoning.
	exec(`DELETE FROM dispatch WHERE command_id='uncertain-command'`)
	assertRetainedEvidence(t, db)
	getReceipt("uncertain-command", false, receipt{"submit_input", "UNKNOWN", number(1), number(8)})

	exec(`INSERT INTO session (session_id,sandbox_name,sandbox_uid,spec,state_value,activity,activity_epoch,
		execution_identity_name,execution_identity_uid,created_at,updated_at)
		VALUES ('mixed-session','mixed-environment',$1,decode('0801','hex'),1,decode('0802','hex'),$1,
			'identity',$1,now(),now())`, uid)
	exec(`INSERT INTO session_delivery_lease (session_id,holder,expires_at)
		VALUES ('mixed-session',$1,now() + interval '1 hour')`, uid)
	getSessionVersion := func(wantText string, wantNumber int32) int64 {
		t.Helper()
		var gotText, activityEpoch, leader string
		var gotNumber int32
		var activity []byte
		var version int64
		if err := db.QueryRowContext(ctx, `SELECT state,state_value,activity,activity_epoch,holder,version
			FROM session JOIN session_delivery_lease USING(session_id) WHERE session_id='mixed-session'`).
			Scan(&gotText, &gotNumber, &activity, &activityEpoch, &leader, &version); err != nil {
			t.Fatal(err)
		}
		if gotText != wantText || gotNumber != wantNumber || !bytes.Equal(activity, []byte{8, 2}) ||
			activityEpoch != uid || leader != uid {
			t.Fatalf("unexpected session: %s/%d activity=%x epoch=%s leader=%s", gotText, gotNumber, activity, activityEpoch, leader)
		}
		return version
	}
	waiting := getSessionVersion("WAITING", 1)
	expectConflict(`UPDATE session SET state='ACCEPTED',state_value=2 WHERE session_id='mixed-session'`)
	expectConflict(`UPDATE session SET state_value=0 WHERE session_id='mixed-session'`)
	exec(`UPDATE session SET state='IN_PROGRESS' WHERE session_id='mixed-session'`)
	inProgress := getSessionVersion("IN_PROGRESS", 2)
	exec(`UPDATE session SET state_value=3 WHERE session_id='mixed-session'`)
	accepted := getSessionVersion("ACCEPTED", 3)
	if inProgress != waiting+1 || accepted != inProgress+1 {
		t.Fatalf("session version sequence = %d, %d, %d", waiting, inProgress, accepted)
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.ExecContext(ctx, `UPDATE session SET state_value=4 WHERE session_id='mixed-session'`); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if version := getSessionVersion("ACCEPTED", 3); version != accepted {
		t.Fatalf("rolled back numeric write advanced session version: %d to %d", accepted, version)
	}
	var counter int64
	if err := db.QueryRowContext(ctx, "SELECT version FROM session_watch_version WHERE singleton").Scan(&counter); err != nil || counter != accepted {
		t.Fatalf("rolled back Session watch counter = %d, want %d: %v", counter, accepted, err)
	}
}

func TestInterruptedNumericReceiptIndexBuildResumes(t *testing.T) {
	db := openMigrationDatabase(t)
	ctx := t.Context()
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(ctx, 32); err != nil {
		t.Fatal(err)
	}
	insertRetainedEvidence(t, db)
	if _, err := db.ExecContext(ctx, `CREATE INDEX CONCURRENTLY ix_dispatch_active_session_position_value
		ON dispatch ((1/(length(command_id)-length(command_id))))`); err == nil {
		t.Fatal("expected invalid concurrent index")
	}
	if _, err := db.ExecContext(ctx, `CREATE INDEX CONCURRENTLY ix_dispatch_session_state_value
		ON dispatch (session_id,state_value)`); err != nil {
		t.Fatal(err)
	}
	const indexOID = "SELECT 'ix_dispatch_session_state_value'::regclass::oid"
	var oldOID, newOID int64
	if err := db.QueryRowContext(ctx, indexOID).Scan(&oldOID); err != nil {
		t.Fatal(err)
	}
	if err := Up(ctx, db); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, indexOID).Scan(&newOID); err != nil || newOID != oldOID {
		t.Fatalf("completed index OID = %d, want %d: %v", newOID, oldOID, err)
	}
	var count int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM pg_index WHERE indisvalid AND indexrelid IN (
		'ix_dispatch_session_state_value'::regclass, 'ix_dispatch_sandbox_uid_state_value'::regclass,
		'ix_dispatch_active_session_position_value'::regclass, 'ix_dispatch_recent_session_state_value_version'::regclass,
		'ix_dispatch_tombstone_session_state_value_version'::regclass,
		'ix_dispatch_active_session_position'::regclass, 'ix_dispatch_recent_session_state_version'::regclass,
		'ix_dispatch_tombstone_session_state_version'::regclass)`).Scan(&count); err != nil || count != 8 {
		t.Fatalf("valid old and numeric receipt indexes = %d, want 8: %v", count, err)
	}
}
