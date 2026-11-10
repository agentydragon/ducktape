package journal

import (
	"bytes"
	"database/sql"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
)

func TestJournalBaselineCreatesFinalSchema(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	journal, err := Open(t.Context(), path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}
	db := openJournalFile(t, path)
	var version int
	if err := db.QueryRowContext(t.Context(), "SELECT max(version_id) FROM "+journalMigrationTableName).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 4 {
		t.Fatalf("journal version = %d, want 4", version)
	}
	for _, table := range []string{"event_entry", "command", "native_request", "native_request_command", "checkpoint", "seal"} {
		var found int
		if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", table).Scan(&found); err != nil {
			t.Fatal(err)
		}
		if found != 1 {
			t.Fatalf("final table %s missing", table)
		}
	}
}

func TestJournalBaselineAdoptsVersionThreeAndKeepsEvidence(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	journal, err := Open(t.Context(), path, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	command := &commandpb.Command{CommandId: "retained-command", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "retained input"}}}
	if _, admitted, err := journal.Admit(t.Context(), command, ""); err != nil || !admitted {
		t.Fatalf("admit: %t, %v", admitted, err)
	}
	if planned, err := journal.DispatchPlanned(t.Context(), command.CommandId); err != nil || !planned {
		t.Fatalf("plan dispatch: %t, %v", planned, err)
	}
	if err := journal.RecordNativeRequest(t.Context(), "retained-request", harness.ClaudeSubmit, []string{"retained-command"}); err != nil {
		t.Fatal(err)
	}
	var original []byte
	if err := journal.conn.QueryRowContext(t.Context(), "SELECT payload FROM event_entry WHERE cursor=1").Scan(&original); err != nil {
		t.Fatal(err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}
	db := openJournalFile(t, path)
	setVersionThreeHistory(t, db)
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	adopted, err := Open(t.Context(), path, "test-source")
	if err != nil {
		t.Fatal("adopt complete predecessor:", err)
	}
	defer adopted.Close()
	var retained []byte
	if err := adopted.conn.QueryRowContext(t.Context(), "SELECT payload FROM event_entry WHERE cursor=1").Scan(&retained); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(retained, original) {
		t.Fatal("adoption rewrote retained EventEntry")
	}
	requests, err := adopted.PendingNativeRequests(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(requests) != 1 || requests[0].ID != "retained-request" || len(requests[0].Commands) != 1 || requests[0].Commands[0].Command.GetCommandId() != command.GetCommandId() {
		t.Fatalf("native request evidence changed: %+v", requests)
	}
	var version int
	if err := adopted.conn.QueryRowContext(t.Context(), "SELECT max(version_id) FROM "+journalMigrationTableName).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 4 {
		t.Fatalf("adopted version = %d", version)
	}
	for version := 1; version <= 3; version++ {
		var count int
		if err := adopted.conn.QueryRowContext(t.Context(), "SELECT count(*) FROM "+journalMigrationTableName+" WHERE version_id=? AND is_applied=1", version).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("lost historical version %d", version)
		}
	}
}

func TestJournalBaselineRejectsIncompleteOrCorruptPredecessor(t *testing.T) {
	for _, test := range []struct {
		name   string
		damage func(*testing.T, *sql.DB)
	}{
		{"partial_history", func(t *testing.T, db *sql.DB) {
			if _, err := db.ExecContext(t.Context(), "DELETE FROM "+journalMigrationTableName+" WHERE version_id=2"); err != nil {
				t.Fatal(err)
			}
		}},
		{"missing_evidence_table", func(t *testing.T, db *sql.DB) {
			if _, err := db.ExecContext(t.Context(), "DROP TABLE native_request_command"); err != nil {
				t.Fatal(err)
			}
		}},
		{"bad_foreign_key", func(t *testing.T, db *sql.DB) {
			if _, err := db.ExecContext(t.Context(), "INSERT INTO native_request_command(request_id,command_id) VALUES('missing-request','missing-command')"); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "journal.sqlite")
			journal, err := Open(t.Context(), path, "test-source")
			if err != nil {
				t.Fatal(err)
			}
			if err := journal.Close(); err != nil {
				t.Fatal(err)
			}
			db := openJournalFile(t, path)
			setVersionThreeHistory(t, db)
			test.damage(t, db)
			if err := db.Close(); err != nil {
				t.Fatal(err)
			}
			if reopened, err := Open(t.Context(), path, "test-source"); err == nil {
				reopened.Close()
				t.Fatal("incomplete predecessor was adopted")
			} else if !strings.Contains(err.Error(), "previous Runtime image") {
				t.Fatalf("rejection does not explain recovery: %v", err)
			}
			db = openJournalFile(t, path)
			var version int
			if err := db.QueryRowContext(t.Context(), "SELECT max(version_id) FROM "+journalMigrationTableName).Scan(&version); err != nil {
				t.Fatal(err)
			}
			if version == 4 {
				t.Fatal("rejected predecessor was marked as migrated")
			}
		})
	}
}

func TestJournalBaselineRejectsUnknownSchemaWithoutHistory(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.sqlite")
	db := openJournalFile(t, path)
	if _, err := db.ExecContext(t.Context(), "CREATE TABLE mystery (value TEXT)"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if journal, err := Open(t.Context(), path, "test-source"); err == nil {
		journal.Close()
		t.Fatal("unknown journal was adopted")
	} else if !strings.Contains(err.Error(), "previous Runtime image") {
		t.Fatalf("unknown journal error = %v", err)
	}
}

func openJournalFile(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func setVersionThreeHistory(t *testing.T, db *sql.DB) {
	t.Helper()
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(t.Context(), "DELETE FROM "+journalMigrationTableName+" WHERE version_id=4"); err != nil {
		t.Fatal(err)
	}
	for version := 1; version <= 3; version++ {
		if _, err := tx.ExecContext(t.Context(), "INSERT INTO "+journalMigrationTableName+" (version_id,is_applied) VALUES(?,1)", version); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
}
