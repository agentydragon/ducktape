package migrations

import (
	"database/sql"
	"strings"
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

// prepareVersion10 exercises adoption against the current schema with completed predecessor history.
func prepareVersion10(t *testing.T, db *sql.DB) {
	t.Helper()
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(t.Context(), `DELETE FROM history_service_goose_version WHERE version_id=11`); err != nil {
		t.Fatal(err)
	}
	for version := 1; version <= 10; version++ {
		if _, err := tx.ExecContext(t.Context(), `INSERT INTO history_service_goose_version(version_id,is_applied) VALUES($1,true)`, version); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
}

func upBaseline(t *testing.T, db *sql.DB) error {
	t.Helper()
	provider, err := newProvider(db)
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

func TestFinalBaselineAdoptsCompletedHistoryWithoutChangingRows(t *testing.T) {
	db := baselineDB(t)
	prepareVersion10(t, db)
	const id = "00000000-0000-4000-8000-000000000012"
	var position int64
	if err := db.QueryRowContext(t.Context(), `INSERT INTO session_history(id,last_cursor,feed_state) VALUES($1,1,$2) RETURNING recorded_position`, id, []byte("recorded-feed")).Scan(&position); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,1,$2,7)`, id, []byte("recorded-event")); err != nil {
		t.Fatal(err)
	}
	var pending int64
	if err := db.QueryRowContext(t.Context(), `SELECT version FROM session_fold_pending WHERE session_id=$1`, id).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	var gotPosition, gotPending int64
	var feed, event []byte
	if err := db.QueryRowContext(t.Context(), `SELECT h.recorded_position,h.feed_state,p.version,e.payload
        FROM session_history h JOIN session_fold_pending p ON p.session_id=h.id
        JOIN session_event e ON e.session_id=h.id WHERE h.id=$1`, id).Scan(&gotPosition, &feed, &gotPending, &event); err != nil {
		t.Fatal(err)
	}
	if gotPosition != position || gotPending != pending || string(feed) != "recorded-feed" || string(event) != "recorded-event" {
		t.Fatalf("adoption changed retained history: position=%d feed=%q pending=%d event=%q", gotPosition, feed, gotPending, event)
	}
	if got := versionCount(t, db, 10); got != 1 {
		t.Fatalf("predecessor version records = %d", got)
	}
	if got := versionCount(t, db, 11); got != 1 {
		t.Fatalf("baseline version records = %d", got)
	}
}

func TestFinalBaselineRejectsIncompletePredecessor(t *testing.T) {
	for _, test := range []struct {
		name    string
		prepare func(*testing.T, *sql.DB)
	}{
		{"version nine", func(t *testing.T, db *sql.DB) {
			prepareVersion10(t, db)
			if _, err := db.ExecContext(t.Context(), `DELETE FROM history_service_goose_version WHERE version_id=10`); err != nil {
				t.Fatal(err)
			}
		}},
		{"missing applied version", func(t *testing.T, db *sql.DB) {
			prepareVersion10(t, db)
			if _, err := db.ExecContext(t.Context(), `DELETE FROM history_service_goose_version WHERE version_id=7`); err != nil {
				t.Fatal(err)
			}
		}},
		{"incomplete schema", func(t *testing.T, db *sql.DB) {
			prepareVersion10(t, db)
			if _, err := db.ExecContext(t.Context(), `DROP INDEX ix_session_event_kind_cursor`); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			db := baselineDB(t)
			test.prepare(t, db)
			err := upBaseline(t, db)
			if err == nil || !strings.Contains(err.Error(), "previous image") {
				t.Fatalf("baseline error = %v", err)
			}
			if got := versionCount(t, db, 11); got != 0 {
				t.Fatalf("failed adoption records = %d", got)
			}
		})
	}
}
