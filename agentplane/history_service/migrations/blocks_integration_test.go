package migrations

import "testing"

func TestEventBlockMigrationPreservesExistingHistoryAndStartsDisabled(t *testing.T) {
	db := baselineDB(t)
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	const id = "00000000-0000-4000-8000-000000000016"
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(id,last_cursor) VALUES($1,1)`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,1,$2,20)`, id, []byte("retained source bytes")); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := Up(t.Context(), db); err != nil {
			t.Fatal(err)
		}
	}
	var payload []byte
	var compactable bool
	if err := db.QueryRowContext(t.Context(), `SELECT payload,compactable FROM session_event WHERE session_id=$1`, id).Scan(&payload, &compactable); err != nil {
		t.Fatal(err)
	}
	if string(payload) != "retained source bytes" || compactable {
		t.Fatalf("retained payload %q, compactable %t", payload, compactable)
	}
	if versionCount(t, db, 16) != 1 {
		t.Fatal("event-block migration not recorded exactly once")
	}
}
