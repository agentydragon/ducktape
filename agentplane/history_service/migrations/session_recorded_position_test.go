package migrations

import (
	"database/sql"
	"strings"
	"testing"

	"github.com/google/uuid"
)

func TestSessionRecordedPositionMigrationPreservesCursorAndIdentity(t *testing.T) {
	db, err := sql.Open("postgres", migrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	provider, err := legacyProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 8); err != nil {
		t.Fatal(err)
	}

	ids := []uuid.UUID{uuid.New(), uuid.New()}
	positions := make([]int64, len(ids))
	for i, id := range ids {
		if err := db.QueryRowContext(t.Context(), `INSERT INTO session_history(id,last_cursor) VALUES($1,0) RETURNING opened_position`, id).Scan(&positions[i]); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatal(err)
	}

	for i, id := range ids {
		var got int64
		if err := db.QueryRowContext(t.Context(), `SELECT recorded_position FROM session_history WHERE id=$1`, id).Scan(&got); err != nil {
			t.Fatal(err)
		}
		if got != positions[i] {
			t.Fatalf("recorded position for %s = %d, want %d", id, got, positions[i])
		}
	}
	var sequence string
	if err := db.QueryRowContext(t.Context(), `SELECT pg_get_serial_sequence('session_history', 'recorded_position')`).Scan(&sequence); err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(sequence, "session_history_recorded_position_seq") {
		t.Fatalf("recorded identity sequence = %q", sequence)
	}
	var next int64
	if err := db.QueryRowContext(t.Context(), `INSERT INTO session_history(id,last_cursor) VALUES($1,0) RETURNING recorded_position`, uuid.New()).Scan(&next); err != nil {
		t.Fatal(err)
	}
	if next <= positions[1] {
		t.Fatalf("next recorded position = %d, want greater than %d", next, positions[1])
	}
	for _, index := range []string{"ux_history_recorded_position", "ix_history_execution_principal_recorded"} {
		var definition string
		if err := db.QueryRowContext(t.Context(), `SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename='session_history' AND indexname=$1`, index).Scan(&definition); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(definition, "recorded_position") {
			t.Fatalf("index %s definition = %q", index, definition)
		}
	}
}
