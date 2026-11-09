package migrations

import (
	"database/sql"
	"testing"
)

func baselineDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", migrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func upBaseline(t *testing.T, db *sql.DB) error {
	t.Helper()
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	_, err = provider.Up(t.Context())
	return err
}

func versionCount(t *testing.T, db *sql.DB, version int) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM history_service_goose_version WHERE version_id=$1 AND is_applied`, version).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestFinalBaselineCreatesHistorySchema(t *testing.T) {
	db := baselineDB(t)
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	if got := versionCount(t, db, 11); got != 1 {
		t.Fatalf("baseline version records = %d", got)
	}
	const id = "00000000-0000-4000-8000-000000000011"
	var position int64
	if err := db.QueryRowContext(t.Context(), `INSERT INTO session_history(id,last_cursor) VALUES($1,0) RETURNING recorded_position`, id).Scan(&position); err != nil {
		t.Fatal(err)
	}
	if position < 1 {
		t.Fatalf("recorded position = %d", position)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,1,$2,3)`, id, []byte("retained")); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_entity(thread_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,state,entity_index)
        VALUES($1,'epoch','item','one',1,1,false,'{}',1)`, id); err != nil {
		t.Fatal(err)
	}
	var pending int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM session_fold_pending WHERE session_id=$1`, id).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if pending != 1 {
		t.Fatalf("new session pending folds = %d", pending)
	}
	var published int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM pg_publication_tables WHERE pubname='electric_publication_agentplane_history'`).Scan(&published); err != nil {
		t.Fatal(err)
	}
	if published != 4 {
		t.Fatalf("Electric published tables = %d", published)
	}
}
