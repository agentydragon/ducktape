package migrations

import (
	"bytes"
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
