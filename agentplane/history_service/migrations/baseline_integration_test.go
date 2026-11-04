package migrations

import (
	"database/sql"
	"reflect"
	"sort"
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

func upLegacy(t *testing.T, db *sql.DB, version int64) {
	t.Helper()
	provider, err := legacyProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), version); err != nil {
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
	upLegacy(t, db, 10)
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
		{"version nine", func(t *testing.T, db *sql.DB) { upLegacy(t, db, 9) }},
		{"missing applied version", func(t *testing.T, db *sql.DB) {
			upLegacy(t, db, 10)
			if _, err := db.ExecContext(t.Context(), `DELETE FROM history_service_goose_version WHERE version_id=7`); err != nil {
				t.Fatal(err)
			}
		}},
		{"incomplete schema", func(t *testing.T, db *sql.DB) {
			upLegacy(t, db, 10)
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

func catalogRows(t *testing.T, db *sql.DB, query string) []string {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), query)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var result []string
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			t.Fatal(err)
		}
		result = append(result, value)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	sort.Strings(result)
	return result
}

func TestFinalBaselineMatchesCompletedPredecessorSchema(t *testing.T) {
	legacy := baselineDB(t)
	upLegacy(t, legacy, 10)
	fresh := baselineDB(t)
	if err := upBaseline(t, fresh); err != nil {
		t.Fatal(err)
	}
	for _, check := range []struct{ name, query string }{
		{"columns", `SELECT concat_ws('|',table_name,column_name,data_type,is_nullable,
            coalesce(column_default,''),is_identity) FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name <> 'history_service_goose_version'
            ORDER BY table_name,ordinal_position`},
		{"constraints", `SELECT concat_ws('|',c.relname,CASE WHEN p.contype='n' THEN '' ELSE p.conname END,pg_get_constraintdef(p.oid))
            FROM pg_constraint p JOIN pg_class c ON c.oid=p.conrelid
            WHERE c.relnamespace=current_schema()::regnamespace
              AND c.relname <> 'history_service_goose_version' ORDER BY c.relname,p.conname`},
		{"indexes", `SELECT concat_ws('|',indexname,indexdef) FROM pg_indexes
            WHERE schemaname=current_schema() AND tablename <> 'history_service_goose_version'
            ORDER BY indexname`},
		{"triggers", `SELECT concat_ws('|',c.relname,t.tgname,pg_get_triggerdef(t.oid))
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            WHERE c.relnamespace=current_schema()::regnamespace AND NOT t.tgisinternal ORDER BY c.relname,t.tgname`},
		{"replica identity", `SELECT concat_ws('|',relname,relreplident) FROM pg_class
            WHERE relnamespace=current_schema()::regnamespace AND relkind='r'
              AND relname <> 'history_service_goose_version' ORDER BY relname`},
		{"Electric publication", `SELECT concat_ws('|',schemaname,tablename) FROM pg_publication_tables
            WHERE pubname='electric_publication_agentplane_history' ORDER BY schemaname,tablename`},
	} {
		t.Run(check.name, func(t *testing.T) {
			want, got := catalogRows(t, legacy, check.query), catalogRows(t, fresh, check.query)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("fresh schema differs from v10:\nwant %v\ngot %v", want, got)
			}
		})
	}
}
