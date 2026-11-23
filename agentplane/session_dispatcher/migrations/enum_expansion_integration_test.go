package migrations

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/lib/pq"
)

func TestEnumExpansionPreservesLegacyWritersAndReceiptVersions(t *testing.T) {
	db := openMigrationDatabase(t)
	before := schema
	before.Files = migrationFilesThrough(t, 30)
	oldProvider, err := before.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := oldProvider.Up(t.Context()); err != nil {
		t.Fatal(err)
	}
	insertRetainedEvidence(t, db)
	insertDispatch := `INSERT INTO dispatch (
		command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at,
		actor_account_namespace,actor_account_name,actor_account_uid,
		effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid%s
	) SELECT $1,session_id,'other-sandbox','00000000-0000-0000-0000-000000000002',$2,command,$3,now(),now(),
		actor_account_namespace,actor_account_name,actor_account_uid,
		effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid%s
	FROM dispatch WHERE command_id = 'uncertain-command'`
	oldInsert := strings.ReplaceAll(insertDispatch, "%s", "")
	execExpansionSQL(t, db, oldInsert, "unmapped-operation", "stop_runner_session", "FAILED")
	execExpansionSQL(t, db, oldInsert, "unmapped-state", "submit_input", "FUTURE_STATE")
	execExpansionSQL(t, db, oldInsert, "unmapped-both", "COMPACT", "failed")
	execExpansionSQL(t, db, oldInsert, "legacy-deleted", "change_model", "CANCELLED_IN_DISPATCHER")
	execExpansionSQL(t, db, "DELETE FROM dispatch WHERE command_id='legacy-deleted'")

	snapshot := func() [5]string {
		t.Helper()
		var values [5]string
		if err := db.QueryRowContext(t.Context(), `SELECT
			(SELECT string_agg(command_id || ':' || version, ',' ORDER BY command_id) FROM dispatch),
			(SELECT string_agg(command_id || ':' || version, ',' ORDER BY command_id) FROM dispatch_tombstone),
			(SELECT string_agg(session_id || ':' || version, ',' ORDER BY session_id) FROM session),
			(SELECT last_value::text FROM dispatch_version_seq),
			(SELECT version::text FROM session_watch_version WHERE singleton)`).Scan(
			&values[0], &values[1], &values[2], &values[3], &values[4],
		); err != nil {
			t.Fatal(err)
		}
		return values
	}
	filenodes := func() [3]int64 {
		t.Helper()
		var nodes [3]int64
		if err := db.QueryRowContext(t.Context(), `SELECT pg_relation_filenode('dispatch'),
			pg_relation_filenode('dispatch_tombstone'), pg_relation_filenode('session')`).Scan(
			&nodes[0], &nodes[1], &nodes[2],
		); err != nil {
			t.Fatal(err)
		}
		return nodes
	}

	priorVersions, priorFiles := snapshot(), filenodes()
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("add enum shadows:", err)
	}
	if filenodes() != priorFiles || snapshot() != priorVersions {
		t.Fatal("schema expansion rewrote tables or changed versions")
	}
	if _, err := oldProvider.Up(t.Context()); err != nil {
		t.Fatal("old replica migration startup against expanded schema:", err)
	}
	assertDispatchEnumColumns(t, db, "dispatch", "uncertain-command", "submit_input", "UNKNOWN", 0, 0)

	// Force the backfill to wait on its first dispatch row. A previous-image writer
	// must still be able to change another dispatch and obtain a normal revision.
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	locked, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer locked.Rollback()
	if _, err := locked.ExecContext(ctx, "SELECT command_id FROM dispatch WHERE command_id='uncertain-command' FOR UPDATE"); err != nil {
		t.Fatal(err)
	}
	blocker := postgres.BackendPID(t, ctx, locked)
	backfill, err := db.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer backfill.Close()
	blocked := postgres.BackendPID(t, ctx, backfill)
	done := make(chan error, 1)
	go func() {
		_, err := backfill.ExecContext(ctx, "CALL backfill_dispatch_enum_values(1)")
		done <- err
	}()
	postgres.WaitForBlocker(t, ctx, db, blocked, blocker)

	if _, err := db.ExecContext(ctx, "UPDATE dispatch SET reason='old writer during backfill' WHERE command_id='unmapped-both'"); err != nil {
		t.Fatal("unrelated text write blocked behind backfill:", err)
	}
	beforeFill := snapshot()
	if beforeFill == priorVersions {
		t.Fatal("ordinary text writer didn't advance the receipt")
	}
	if err := locked.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if got := snapshot(); got != beforeFill {
		t.Fatalf("backfill changed receipt or Session checkpoints: %v, before %v", got, beforeFill)
	}

	assertRetainedEvidence(t, db)
	assertDispatchEnumColumns(t, db, "dispatch", "uncertain-command", "submit_input", "UNKNOWN", 1, 8)
	assertDispatchEnumColumns(t, db, "dispatch", "unmapped-operation", "stop_runner_session", "FAILED", 0, 6)
	assertDispatchEnumColumns(t, db, "dispatch", "unmapped-state", "submit_input", "FUTURE_STATE", 1, 0)
	assertDispatchEnumColumns(t, db, "dispatch", "unmapped-both", "COMPACT", "failed", 0, 0)
	assertDispatchEnumColumns(t, db, "dispatch_tombstone", "legacy-deleted", "change_model", "CANCELLED_IN_DISPATCHER", 2, 7)

	assertSessionEnumColumn := func(wantText string, want int32) {
		t.Helper()
		var text string
		var number int32
		if err := db.QueryRowContext(t.Context(), "SELECT state,state_value FROM session WHERE session_id='retained-session'").Scan(&text, &number); err != nil {
			t.Fatal(err)
		}
		if text != wantText || number != want {
			t.Fatalf("Session materialization = (%q, %d), want (%q, %d)", text, number, wantText, want)
		}
	}
	assertSessionEnumColumn("UNKNOWN", 5)

	execExpansionSQL(t, db, oldInsert, "old-writer", "interrupt_turn", "QUEUED_IN_DISPATCHER")
	assertDispatchEnumColumns(t, db, "dispatch", "old-writer", "interrupt_turn", "QUEUED_IN_DISPATCHER", 5, 1)
	execExpansionSQL(t, db, "UPDATE dispatch SET state='DELIVERING_TO_RUNNER' WHERE command_id='old-writer'")
	execExpansionSQL(t, db, "UPDATE dispatch SET reason='legacy update' WHERE command_id='unmapped-both'")
	execExpansionSQL(t, db, "UPDATE session SET state='IN_PROGRESS' WHERE session_id='retained-session'")
	assertDispatchEnumColumns(t, db, "dispatch", "old-writer", "interrupt_turn", "DELIVERING_TO_RUNNER", 5, 2)
	assertDispatchEnumColumns(t, db, "dispatch", "unmapped-both", "COMPACT", "failed", 0, 0)
	assertSessionEnumColumn("IN_PROGRESS", 2)

	typedInsert := strings.Replace(strings.Replace(insertDispatch, "%s", ",operation_value,state_value", 1), "%s", ",$4,$5", 1)
	execExpansionSQL(t, db, typedInsert, "new-writer", "change_reasoning_effort", "QUEUED_IN_DISPATCHER", 3, 1)
	execExpansionSQL(t, db, "UPDATE dispatch SET state='RUNNER_ADMITTED',state_value=3 WHERE command_id='new-writer'")
	execExpansionSQL(t, db, "UPDATE session SET state='ACCEPTED',state_value=3 WHERE session_id='retained-session'")
	assertDispatchEnumColumns(t, db, "dispatch", "new-writer", "change_reasoning_effort", "RUNNER_ADMITTED", 3, 3)
	assertSessionEnumColumn("ACCEPTED", 3)

	checkViolation := func(query string, args ...any) {
		t.Helper()
		_, err := db.ExecContext(t.Context(), query, args...)
		var pgErr *pq.Error
		if !errors.As(err, &pgErr) || pgErr.Code != "23514" {
			t.Fatalf("want check violation, got %v from %s", err, query)
		}
	}
	checkViolation(typedInsert, "inconsistent-insert", "compact", "UNKNOWN", 6, 8)
	checkViolation("UPDATE dispatch SET state='FAILED',state_value=3 WHERE command_id='new-writer'")
	checkViolation("UPDATE dispatch SET operation_value=NULL WHERE command_id='new-writer'")
	checkViolation("UPDATE dispatch SET state='FAILED',state_value=8 WHERE command_id='unmapped-state'")
	checkViolation("UPDATE session SET state='FAILED',state_value=3 WHERE session_id='retained-session'")
	checkViolation("UPDATE session SET state='FUTURE_STATE' WHERE session_id='retained-session'")
	checkViolation("UPDATE dispatch_tombstone SET operation_value=5 WHERE command_id='legacy-deleted'")

	execExpansionSQL(t, db, "DELETE FROM dispatch WHERE command_id='old-writer'")
	assertDispatchEnumColumns(t, db, "dispatch_tombstone", "old-writer", "interrupt_turn", "DELIVERING_TO_RUNNER", 5, 2)
	if got := snapshot(); got == priorVersions {
		t.Fatal("business writes and deletion did not advance the checkpoints")
	}
	beforeRepeat := snapshot()
	execExpansionSQL(t, db, "CALL backfill_dispatch_enum_values(2)")
	if snapshot() != beforeRepeat {
		t.Fatal("repeating the backfill changed a revision")
	}
}

func TestEnumExpansionMatchesPublicEnumNumbers(t *testing.T) {
	db := openMigrationDatabase(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		function string
		prefix   string
		values   map[string]int32
		lower    bool
		count    int
	}{
		{"dispatch_operation_value", "OPERATION_", commandpb.Operation_value, true, 4},
		{"dispatch_state_value", "DELIVERY_STATE_", dispatcherpb.DeliveryState_value, false, 6},
		{"session_materialization_value", "SESSION_MATERIALIZATION_", dispatcherpb.SessionStatus_Materialization_value, false, 5},
	}
	for _, tc := range cases {
		t.Run(tc.function, func(t *testing.T) {
			count := 0
			for name, number := range tc.values {
				spelling := strings.TrimPrefix(name, tc.prefix)
				if tc.lower {
					spelling = strings.ToLower(spelling)
				}
				var value sql.NullInt32
				if err := db.QueryRowContext(t.Context(), "SELECT "+tc.function+"($1)", spelling).Scan(&value); err != nil {
					t.Fatal(err)
				}
				accepted := number != 0 && name != "OPERATION_COMPACT"
				if value.Valid != accepted || (value.Valid && value.Int32 != number) {
					t.Errorf("lookup %q: %v, want %d with accepted=%v", spelling, value, number, accepted)
				}
				if value.Valid {
					count++
				}
			}
			if count != tc.count {
				t.Fatalf("mapped %d members, want %d", count, tc.count)
			}
		})
	}
}

func execExpansionSQL(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatalf("%s: %v", query, err)
	}
}

func assertDispatchEnumColumns(t *testing.T, db *sql.DB, table, id, wantOperation, wantState string, wantOpValue, wantStateValue int32) {
	t.Helper()
	var operation, state string
	var opValue, stateValue sql.NullInt32
	err := db.QueryRowContext(t.Context(), "SELECT operation,state,operation_value,state_value FROM "+table+" WHERE command_id=$1", id).
		Scan(&operation, &state, &opValue, &stateValue)
	if err != nil {
		t.Fatal(err)
	}
	if operation != wantOperation || state != wantState ||
		opValue != (sql.NullInt32{Int32: wantOpValue, Valid: wantOpValue != 0}) ||
		stateValue != (sql.NullInt32{Int32: wantStateValue, Valid: wantStateValue != 0}) {
		t.Fatalf("%s %q = (%q, %q, %v, %v), want (%q, %q, %d, %d)", table, id,
			operation, state, opValue, stateValue, wantOperation, wantState, wantOpValue, wantStateValue)
	}
}
