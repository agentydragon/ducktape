package migrations

import (
	"bytes"
	"context"
	"database/sql"
	"testing"
	"time"

	_ "github.com/lib/pq"
)

func TestUpgradeFromVersionTwoDropsStopDispatches(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 2); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	for _, row := range []struct {
		commandID string
		operation string
	}{
		{commandID: "stop-1", operation: "stop_runner_session"},
		{commandID: "send-1", operation: "submit_input"},
	} {
		_, err := db.ExecContext(t.Context(), `
			INSERT INTO dispatch (
				command_id, session_id, sandbox_name, sandbox_uid, operation,
				command, state, created_at, updated_at
			) VALUES ($1, 'session', 'sandbox', '00000000-0000-0000-0000-000000000001', $2,
				$3, 'RUNNER_ADMITTED', now(), now())`, row.commandID, row.operation, []byte{})
		if err != nil {
			t.Fatalf("insert %s dispatch: %v", row.operation, err)
		}
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatal("upgrade predecessor schema:", err)
	}
	var count int
	if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM dispatch").Scan(&count); err != nil {
		t.Fatal("count upgraded dispatches:", err)
	}
	if count != 1 {
		t.Fatalf("remaining dispatch count = %d, want 1", count)
	}
	var commandID string
	if err := db.QueryRowContext(t.Context(), "SELECT command_id FROM dispatch").Scan(&commandID); err != nil {
		t.Fatal("read upgraded dispatch:", err)
	}
	if commandID != "send-1" {
		t.Fatalf("remaining dispatch command_id = %q, want send-1", commandID)
	}
}

func TestUpgradeFromVersionThreeClearsEmptyAdmissions(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 3); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	now := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)
	for _, row := range []struct {
		commandID string
		state     string
		admission []byte
	}{
		{commandID: "claimed", state: "DELIVERING_TO_RUNNER", admission: []byte{}},
		{commandID: "admitted", state: "RUNNER_ADMITTED", admission: []byte{0x08, 0x01}},
	} {
		_, err := db.ExecContext(t.Context(), `
			INSERT INTO dispatch (
				command_id, session_id, sandbox_name, sandbox_uid, operation,
				command, state, admission, created_at, updated_at
			) VALUES ($1, 'session', 'sandbox', '00000000-0000-0000-0000-000000000001', 'submit_input',
				$2, $3, $4, $5, $5)`, row.commandID, []byte{}, row.state, row.admission, now)
		if err != nil {
			t.Fatalf("insert %s dispatch: %v", row.commandID, err)
		}
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatal("upgrade predecessor schema:", err)
	}
	rows, err := db.QueryContext(t.Context(), "SELECT command_id, admission IS NULL, admission FROM dispatch ORDER BY command_id")
	if err != nil {
		t.Fatal("read upgraded admissions:", err)
	}
	defer rows.Close()
	got := make(map[string]struct {
		isNull bool
		data   []byte
	})
	for rows.Next() {
		var commandID string
		var value struct {
			isNull bool
			data   []byte
		}
		if err := rows.Scan(&commandID, &value.isNull, &value.data); err != nil {
			t.Fatal("scan upgraded admission:", err)
		}
		got[commandID] = value
	}
	if err := rows.Err(); err != nil {
		t.Fatal("iterate upgraded admissions:", err)
	}
	if len(got) != 2 || !got["claimed"].isNull || got["admitted"].isNull || !bytes.Equal(got["admitted"].data, []byte{0x08, 0x01}) {
		t.Fatalf("upgraded admissions = %#v, want claimed NULL and admitted 0801", got)
	}
}

func TestWakeAssessmentDefaultsSupportPreviousWritersDuringUpgrade(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 7); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	if err := insertPreviousDispatchWriterRow(t.Context(), db, "dispatch-before-upgrade"); err != nil {
		t.Fatal("insert predecessor dispatch:", err)
	}
	if err := insertPreviousOpeningWriterRow(t.Context(), db, "opening-before-upgrade"); err != nil {
		t.Fatal("insert predecessor opening:", err)
	}
	if _, err := provider.UpTo(t.Context(), 8); err != nil {
		t.Fatal("apply wake assessment migration:", err)
	}

	// These statements match the predecessor Gateway's INSERT column sets. Old
	// replicas omit both new columns while they overlap the upgraded schema.
	if err := insertPreviousDispatchWriterRow(t.Context(), db, "dispatch-during-upgrade"); err != nil {
		t.Fatal("previous dispatch writer after upgrade:", err)
	}
	if err := insertPreviousOpeningWriterRow(t.Context(), db, "opening-during-upgrade"); err != nil {
		t.Fatal("previous opening writer after upgrade:", err)
	}
	if err := insertCurrentDispatchWriterRow(t.Context(), db, "dispatch-current-writer"); err != nil {
		t.Fatal("current dispatch writer after upgrade:", err)
	}
	if err := insertCurrentOpeningWriterRow(t.Context(), db, "opening-current-writer"); err != nil {
		t.Fatal("current opening writer after upgrade:", err)
	}

	for _, row := range []struct {
		table string
		key   string
		id    string
	}{
		{table: "dispatch", key: "command_id", id: "dispatch-before-upgrade"},
		{table: "session_opening", key: "session_id", id: "opening-before-upgrade"},
		{table: "dispatch", key: "command_id", id: "dispatch-during-upgrade"},
		{table: "session_opening", key: "session_id", id: "opening-during-upgrade"},
	} {
		var wake, blocker []byte
		query := "SELECT wake_assessment, delivery_blocker FROM " + row.table + " WHERE " + row.key + "=$1"
		if err := db.QueryRowContext(t.Context(), query, row.id).Scan(&wake, &blocker); err != nil {
			t.Fatalf("read previous-writer row %s: %v", row.id, err)
		}
		if !bytes.Equal(wake, []byte{0x08, 0x01}) || blocker != nil {
			t.Fatalf("previous-writer row %s = wake %x, blocker %x; want NOT_EVALUATED and NULL", row.id, wake, blocker)
		}
	}
	for _, row := range []struct {
		table string
		key   string
		id    string
	}{
		{table: "dispatch", key: "command_id", id: "dispatch-current-writer"},
		{table: "session_opening", key: "session_id", id: "opening-current-writer"},
	} {
		var wake []byte
		query := "SELECT wake_assessment FROM " + row.table + " WHERE " + row.key + "=$1"
		if err := db.QueryRowContext(t.Context(), query, row.id).Scan(&wake); err != nil {
			t.Fatalf("read current-writer row %s: %v", row.id, err)
		}
		if !bytes.Equal(wake, []byte{0x08, 0x02}) {
			t.Fatalf("current-writer row %s wake = %x, want explicit NOT_NEEDED", row.id, wake)
		}
	}
}

func insertPreviousDispatchWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,wake_eligible,wake_caller_namespace,wake_caller_name,
		 operation,command,state,traceparent,actor_account_namespace,actor_account_name,actor_account_uid,actor_pod_name,actor_pod_uid,
		 effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,action_request_id,action_execution_id,
		 created_at,updated_at)
		VALUES ($1,'session','sandbox','00000000-0000-0000-0000-000000000001',false,NULL,NULL,
		 'submit_input',decode('','hex'),'QUEUED_IN_DISPATCHER','',NULL,NULL,NULL,NULL,NULL,
		 NULL,NULL,NULL,NULL,NULL,now(),now())`, id)
	return err
}

func insertPreviousOpeningWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO session_opening
		(session_id,sandbox_name,sandbox_uid,execution_account_namespace,execution_account_name,execution_account_uid,
		 creator_account_namespace,creator_account_name,creator_account_uid,effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,
		 creator_pod_name,creator_pod_uid,action_request_id,action_execution_id,wake_eligible,wake_caller_namespace,wake_caller_name,
		 spec,setup_script,state,traceparent,created_at,updated_at)
		VALUES ($1,'sandbox','00000000-0000-0000-0000-000000000001',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
		 NULL,NULL,NULL,NULL,false,NULL,NULL,decode('','hex'),NULL,'QUEUED_IN_DISPATCHER','',now(),now())`, id)
	return err
}

func insertCurrentDispatchWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at,delivery_blocker,wake_assessment)
		VALUES ($1,'session','sandbox','00000000-0000-0000-0000-000000000001','submit_input',decode('','hex'),
		 'QUEUED_IN_DISPATCHER',now(),now(),NULL,decode('0802','hex'))`, id)
	return err
}

func insertCurrentOpeningWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO session_opening
		(session_id,sandbox_name,sandbox_uid,spec,state,created_at,updated_at,delivery_blocker,wake_assessment)
		VALUES ($1,'sandbox','00000000-0000-0000-0000-000000000001',decode('','hex'),
		 'QUEUED_IN_DISPATCHER',now(),now(),NULL,decode('0802','hex'))`, id)
	return err
}

func openMigrationDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal("open PostgreSQL clone:", err)
	}
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Errorf("close PostgreSQL clone: %v", err)
		}
	})
	return db
}
