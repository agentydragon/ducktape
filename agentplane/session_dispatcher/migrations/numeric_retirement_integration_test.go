package migrations

import (
	"database/sql"
	"errors"
	"strings"
	"testing"

	"github.com/lib/pq"
)

func prepareEnumRetirement(t *testing.T, db *sql.DB) {
	t.Helper()
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 30); err != nil {
		t.Fatal(err)
	}
	insertRetainedEvidence(t, db)
	execRetirementSQL(t, db, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,reason,
		 admission,delivery_blocker,wake_assessment,created_at,updated_at)
		VALUES ('old-tombstone','retained-session','historical-env','00000000-0000-0000-0000-000000000034',
		 'interrupt_turn',decode('0801','hex'),'FAILED','historical-reason',
		 decode('0802','hex'),decode('0803','hex'),decode('0804','hex'),now(),now())`)
	execRetirementSQL(t, db, `DELETE FROM dispatch WHERE command_id='old-tombstone'`)
	execRetirementSQL(t, db, `UPDATE dispatch_watch_retention SET pruned_through=1 WHERE singleton`)
	if err := upWithEnumBackfill(t, db); err != nil {
		t.Fatal(err)
	}
}

func execRetirementSQL(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query); err != nil {
		t.Fatalf("%s: %v", query, err)
	}
}

func queryRetirementValue[T comparable](t *testing.T, db *sql.DB, query string) T {
	t.Helper()
	var value T
	if err := db.QueryRowContext(t.Context(), query).Scan(&value); err != nil {
		t.Fatalf("%s: %v", query, err)
	}
	return value
}

const legacyEnumColumns = `SELECT count(*) FROM information_schema.columns
    WHERE table_schema=current_schema() AND table_name IN ('dispatch','dispatch_tombstone','session')
    AND column_name IN ('operation','state')`

func TestDispatcherNumericRetirementPreservesEvidenceAndVersions(t *testing.T) {
	db := openMigrationDatabase(t)
	prepareEnumRetirement(t, db)

	const snapshot = `SELECT jsonb_build_array(
		(SELECT jsonb_agg(to_jsonb(d) - ARRAY['operation','state'] ORDER BY command_id) FROM dispatch d),
		(SELECT jsonb_agg(to_jsonb(d) - ARRAY['operation','state'] ORDER BY version) FROM dispatch_tombstone d),
		(SELECT jsonb_agg(to_jsonb(s) - 'state' ORDER BY session_id) FROM session s),
		(SELECT last_value FROM dispatch_version_seq),
		(SELECT version FROM session_watch_version WHERE singleton),
		(SELECT pruned_through FROM dispatch_watch_retention WHERE singleton)
	)::text`
	const numericIndexes = `SELECT array_agg(indexrelid ORDER BY indexrelid)::text FROM pg_index
		WHERE indisvalid AND (indexrelid::regclass)::text IN (
		'ix_dispatch_session_state_value', 'ix_dispatch_sandbox_uid_state_value',
		'ix_dispatch_session_position', 'ix_dispatch_active_session_position_value',
		'ix_dispatch_recent_session_state_value_version', 'ix_dispatch_tombstone_session_state_value_version',
		'ix_dispatch_version','ix_dispatch_tombstone_version','ix_dispatch_tombstone_deleted_at','session_version_idx')
		HAVING count(*)=10`
	before := queryRetirementValue[string](t, db, snapshot)
	indexes := queryRetirementValue[string](t, db, numericIndexes)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if got := queryRetirementValue[string](t, db, snapshot); got != before {
		t.Fatalf("cleanup rewrote retained rows or watch state\nbefore %s\nafter  %s", before, got)
	}
	if got := queryRetirementValue[string](t, db, numericIndexes); got != indexes {
		t.Fatalf("retained index OIDs changed from %s to %s", indexes, got)
	}
	assertRetainedEvidence(t, db)
	if count := queryRetirementValue[int](t, db, legacyEnumColumns); count != 0 {
		t.Fatalf("cleanup left %d legacy enum columns", count)
	}
	const requiredNumericColumns = `SELECT count(*) FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name IN ('dispatch','dispatch_tombstone','session')
        AND column_name IN ('operation_value','state_value') AND data_type='integer' AND is_nullable='NO'`
	if count := queryRetirementValue[int](t, db, requiredNumericColumns); count != 5 {
		t.Fatalf("got %d required numeric enum columns, want 5", count)
	}

	// Numeric writes stamp versions as before, and the original Environment fence still protects the command body.
	previousSessionVersion := queryRetirementValue[int64](t, db,
		`SELECT version FROM session_watch_version WHERE singleton`)
	sessionVersion := queryRetirementValue[int64](t, db,
		`UPDATE session SET state_value=4 WHERE session_id='retained-session' RETURNING version`)
	if got := queryRetirementValue[int64](t, db, `SELECT version FROM session_watch_version WHERE singleton`); got != sessionVersion || got != previousSessionVersion+1 {
		t.Fatalf("session version %d, checkpoint %d, prior checkpoint %d", sessionVersion, got, previousSessionVersion)
	}
	previousDispatchVersion := queryRetirementValue[int64](t, db, `SELECT last_value FROM dispatch_version_seq`)
	dispatchVersion := queryRetirementValue[int64](t, db,
		`UPDATE dispatch SET state_value=6 WHERE command_id='uncertain-command' RETURNING version`)
	if dispatchVersion != previousDispatchVersion+1 {
		t.Fatalf("dispatch version %d, prior version %d", dispatchVersion, previousDispatchVersion)
	}
	execRetirementSQL(t, db, `DELETE FROM dispatch WHERE command_id='uncertain-command'`)
	if got := queryRetirementValue[int64](t, db,
		`SELECT version FROM dispatch WHERE command_id='uncertain-command' AND state_value=6`); got != dispatchVersion {
		t.Fatalf("environment deletion changed protected command version from %d to %d", dispatchVersion, got)
	}

	// A normal deletion copies every receipt field, advances the same watch sequence once,
	// and omits the command body and Sandbox address just as before.
	insertVersion := queryRetirementValue[int64](t, db, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation_value,command,state_value,reason,
		 admission,delivery_blocker,wake_assessment,created_at,updated_at)
		VALUES ('numeric-tombstone','retained-session','numeric-env','00000000-0000-0000-0000-000000000034',
		 5,decode('0801','hex'),7,'numeric-reason',
		 decode('0802','hex'),decode('0803','hex'),decode('0804','hex'),now(),now()) RETURNING version`)
	const receiptColumns = `command_id,session_id,operation_value,state_value,reason,admission,
		delivery_blocker,wake_assessment,created_at,updated_at`
	receipt := queryRetirementValue[string](t, db, `SELECT row(`+receiptColumns+`)::text FROM dispatch WHERE command_id='numeric-tombstone'`)
	execRetirementSQL(t, db, `DELETE FROM dispatch WHERE command_id='numeric-tombstone'`)
	if got := queryRetirementValue[string](t, db, `SELECT row(`+receiptColumns+`)::text FROM dispatch_tombstone WHERE command_id='numeric-tombstone'`); got != receipt {
		t.Fatalf("numeric tombstone changed receipt: %s, want %s", got, receipt)
	}
	if got := queryRetirementValue[int64](t, db, `SELECT version FROM dispatch_tombstone WHERE command_id='numeric-tombstone'`); got != insertVersion+1 {
		t.Fatalf("tombstone version = %d, want %d", got, insertVersion+1)
	}

	// The pre-cleanup numeric binary embeds migrations only through 33.
	numericSchema := schema
	numericSchema.Files = migrationFilesThrough(t, 33)
	if err := numericSchema.Up(t.Context(), db); err != nil {
		t.Fatal("pre-cleanup numeric migration startup:", err)
	}
	_, err := db.ExecContext(t.Context(), `UPDATE dispatch SET state='FAILED' WHERE command_id='uncertain-command'`)
	var pgErr *pq.Error
	if !errors.As(err, &pgErr) || pgErr.Code != "42703" {
		t.Fatalf("legacy write after cleanup returned %v, want undefined_column", err)
	}
	_, err = db.ExecContext(t.Context(), `UPDATE session SET state_value=0 WHERE session_id='retained-session'`)
	if !errors.As(err, &pgErr) || pgErr.Code != "23514" || pgErr.Constraint != "session_materialization_state" {
		t.Fatalf("invalid materialization after cleanup returned %v", err)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("repeat cleanup:", err)
	}
}

func TestDispatcherNumericRetirementRejectsInvalidPairs(t *testing.T) {
	for _, tc := range []struct{ table, trigger, column, value string }{
		{"dispatch", "a_sync_dispatch_enum_values", "operation", "'SUBMIT_INPUT'"},
		{"dispatch", "a_sync_dispatch_enum_values", "state_value", "NULL"},
		{"dispatch_tombstone", "a_sync_dispatch_enum_values", "operation_value", "4"},
		{"dispatch_tombstone", "a_sync_dispatch_enum_values", "state", "'removed-state'"},
		{"session", "a_sync_session_state_value", "state_value", "4"},
	} {
		t.Run(tc.table+"."+tc.column, func(t *testing.T) {
			db := openMigrationDatabase(t)
			prepareEnumRetirement(t, db)
			// Disable the compatibility trigger to simulate a corrupt preexisting pair.
			execRetirementSQL(t, db, "ALTER TABLE "+tc.table+" DISABLE TRIGGER "+tc.trigger)
			execRetirementSQL(t, db, "UPDATE "+tc.table+" SET "+tc.column+"="+tc.value)
			execRetirementSQL(t, db, "ALTER TABLE "+tc.table+" ENABLE TRIGGER "+tc.trigger)
			err := Up(t.Context(), db)
			if err == nil || !strings.Contains(err.Error(), "retire Dispatcher enum text: "+tc.table+" has") {
				t.Fatalf("invalid %s.%s was not reported: %v", tc.table, tc.column, err)
			}
			if version := queryRetirementValue[int64](t, db, `SELECT max(version_id) FROM `+gooseTable+` WHERE is_applied`); version != 33 {
				t.Fatalf("failed cleanup advanced Goose to %d", version)
			}
			if count := queryRetirementValue[int](t, db, legacyEnumColumns); count != 5 {
				t.Fatalf("failed cleanup left only %d of 5 legacy columns", count)
			}
		})
	}
}
