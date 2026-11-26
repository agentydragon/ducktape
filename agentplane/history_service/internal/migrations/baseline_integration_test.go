package migrations

import (
	"database/sql"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func baselineDB(t *testing.T) *sql.DB {
	t.Helper()
	return postgres.Open(t, migrationDatabase.Clone(t))
}

func execute(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

// upTo migrates db to version, for tests that exercise adopting an older schema.
func upTo(t *testing.T, db *sql.DB, version int64) {
	t.Helper()
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), version); err != nil {
		t.Fatal(err)
	}
}

func TestHistoryBaselineCreatesUsableHistory(t *testing.T) {
	db := baselineDB(t)
	for range 2 {
		if err := Up(t.Context(), db); err != nil {
			t.Fatal(err)
		}
	}
	id := uuid.New()
	history := storage.NewStore(db, storage.Options{}).WithReadScope(storage.AllSessions())
	if _, err := history.BindWriter(t.Context(), id, storage.ServiceAccountWriter{Namespace: "fixture", Name: "fixture-writer", UID: "fixture-uid"}, ""); err != nil {
		t.Fatal(err)
	}
	entry := &eventlogpb.EventEntry{Cursor: 1, Origin: &eventlogpb.EventOrigin{SourceId: "fixture-source", Sequence: 1}, Event: &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}}}
	if _, err := history.Append(t.Context(), id, []*eventlogpb.EventEntry{entry}); err != nil {
		t.Fatal(err)
	}
	page, err := history.ReadPage(t.Context(), id, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Entries) != 1 || !proto.Equal(page.Entries[0], entry) {
		t.Fatal("fresh baseline cannot read appended history")
	}
	if postgres.Count(t, db, `SELECT count(*) FROM session_fold_pending WHERE session_id=$1`, id) != 1 {
		t.Fatal("fold trigger did not queue history")
	}
	if postgres.Count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id=18 AND is_applied`) != 1 {
		t.Fatal("baseline not recorded")
	}
	if postgres.Count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id=19 AND is_applied`) != 1 {
		t.Fatal("service-writer migration not recorded")
	}
}

func TestHistoryBaselineAdoptsCompletedRewriteWithoutRecreatingTables(t *testing.T) {
	db := baselineDB(t)
	upTo(t, db, 18)
	id := uuid.New()
	execute(t, db, `INSERT INTO session_history(id,last_cursor,source_id) VALUES($1,3,'fixture-source')`, id)
	execute(t, db, `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,3,$2,31)`, id, []byte("fixture retained event"))
	execute(t, db, `DELETE FROM history_service_goose_version WHERE version_id=18`)
	execute(t, db, `INSERT INTO history_service_goose_version(version_id,is_applied) SELECT generate_series(1,17),true`)
	var relation uint64
	if err := db.QueryRowContext(t.Context(), `SELECT 'session_event_block'::regclass::oid`).Scan(&relation); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := Up(t.Context(), db); err != nil {
			t.Fatal(err)
		}
	}
	if postgres.Count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id BETWEEN 1 AND 18 AND is_applied`) != 18 {
		t.Fatal("adoption rewrote Goose history")
	}
	if postgres.Count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id=19 AND is_applied`) != 1 {
		t.Fatal("adoption did not apply the service-writer migration")
	}
	if postgres.Count(t, db, `SELECT count(*) FROM pg_class WHERE oid=$1 AND relname='session_event_block'`, relation) != 1 {
		t.Fatal("adoption recreated blocks")
	}
	if postgres.Count(t, db, `SELECT count(*) FROM session_event WHERE session_id=$1 AND cursor=3 AND payload=$2`, id, []byte("fixture retained event")) != 1 {
		t.Fatal("adoption changed event bytes")
	}
	if postgres.Count(t, db, `SELECT count(*) FROM session_history WHERE id=$1 AND last_cursor=3 AND source_id='fixture-source'`, id) != 1 {
		t.Fatal("adoption changed session watermark")
	}
}

func TestHistoryBaselineRejectsIncompletePredecessors(t *testing.T) {
	for _, mode := range []string{"unmarked", "before_rewrite", "partial_schema"} {
		t.Run(mode, func(t *testing.T) {
			db := baselineDB(t)
			upTo(t, db, 18)
			execute(t, db, `DELETE FROM history_service_goose_version WHERE version_id>0`)
			if mode != "unmarked" {
				version := 17
				if mode == "before_rewrite" {
					version = 16
				}
				execute(t, db, `INSERT INTO history_service_goose_version(version_id,is_applied) VALUES($1,true)`, version)
			}
			if mode == "partial_schema" {
				execute(t, db, `ALTER TABLE session_event_block DROP COLUMN payload`)
			}
			if err := Up(t.Context(), db); err == nil {
				t.Fatal("accepted incomplete predecessor")
			}
		})
	}
}

func TestHistoryBaselineSerializesConcurrentCreation(t *testing.T) {
	db := baselineDB(t)
	var workers sync.WaitGroup
	failures := make(chan error, 2)
	for range 2 {
		workers.Add(1)
		go func() { defer workers.Done(); failures <- Up(t.Context(), db) }()
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	if postgres.Count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id=18 AND is_applied`) != 1 {
		t.Fatal("baseline recorded more than once")
	}
}
