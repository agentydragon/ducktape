package migrations

import (
	"database/sql"
	"strings"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
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

func execute(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func count(t *testing.T, db *sql.DB, query string, args ...any) int {
	t.Helper()
	var n int
	if err := db.QueryRowContext(t.Context(), query, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
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
	if count(t, db, `SELECT count(*) FROM session_fold_pending WHERE session_id=$1`, id) != 1 {
		t.Fatal("fold trigger did not queue history")
	}
	if count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id=18 AND is_applied`) != 1 {
		t.Fatal("baseline not recorded")
	}
}

func TestHistoryBaselineAdoptsCompletedRewriteWithoutRecreatingTables(t *testing.T) {
	db := baselineDB(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	id := uuid.New()
	execute(t, db, `INSERT INTO session_history(id,last_cursor,source_id,sandbox_uid) VALUES($1,3,'fixture-source','fixture-sandbox-uid')`, id)
	execute(t, db, `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,3,$2,31)`, id, []byte("fixture retained event"))
	execute(t, db, `DELETE FROM history_service_goose_version WHERE version_id>=18`)
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
	if count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id BETWEEN 1 AND 18 AND is_applied`) != 18 {
		t.Fatal("adoption rewrote Goose history")
	}
	if count(t, db, `SELECT count(*) FROM pg_class WHERE oid=$1 AND relname='session_event_block'`, relation) != 1 {
		t.Fatal("adoption recreated blocks")
	}
	if count(t, db, `SELECT count(*) FROM session_event WHERE session_id=$1 AND cursor=3 AND payload=$2`, id, []byte("fixture retained event")) != 1 {
		t.Fatal("adoption changed event bytes")
	}
	if count(t, db, `SELECT count(*) FROM session_history WHERE id=$1 AND last_cursor=3 AND source_id='fixture-source'`, id) != 1 {
		t.Fatal("adoption changed session watermark")
	}
}

func TestHistoryBaselineRejectsIncompletePredecessors(t *testing.T) {
	for _, mode := range []string{"unmarked", "before_rewrite", "partial_schema"} {
		t.Run(mode, func(t *testing.T) {
			db := baselineDB(t)
			if err := Up(t.Context(), db); err != nil {
				t.Fatal(err)
			}
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
	if count(t, db, `SELECT count(*) FROM history_service_goose_version WHERE version_id=18 AND is_applied`) != 1 {
		t.Fatal("baseline recorded more than once")
	}
}

func TestMissingSandboxUIDMigrationDeletesHistoryAndProjection(t *testing.T) {
	db := baselineDB(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	missingUID := uuid.New()
	emptyUID := uuid.New()
	retained := uuid.New()
	for _, session := range []struct {
		id  uuid.UUID
		uid *string
	}{{missingUID, nil}, {emptyUID, stringPointer("")}, {retained, stringPointer("sandbox-uid")}} {
		execute(t, db, `INSERT INTO session_history(id,last_cursor,sandbox_uid) VALUES($1,0,$2)`, session.id, session.uid)
	}
	for _, id := range []uuid.UUID{missingUID, emptyUID} {
		execute(t, db, `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,1,$2,1)`, id, []byte("event"))
		execute(t, db, `INSERT INTO session_event_settlement(session_id,completion_cursor,payload) VALUES($1,1,$2)`, id, []byte("settlement"))
		execute(t, db, `INSERT INTO session_event_settled_range(session_id,first_cursor,last_cursor,completion_cursor) VALUES($1,1,1,1)`, id)
		execute(t, db, `INSERT INTO session_event_removed_range(session_id,first_cursor,last_cursor) VALUES($1,2,2)`, id)
		execute(t, db, `INSERT INTO session_projection_lease(session_id,token,expires_at) VALUES($1,$2,now())`, id, uuid.New())
		execute(t, db, `INSERT INTO session_event_block(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload) VALUES($1,1,1,1,'{1}','{5}',$2)`, id, []byte("block"))
		execute(t, db, `INSERT INTO thread_checkpoint(thread_id,projection_epoch,through_cursor) VALUES($1,'epoch',0)`, id)
		execute(t, db, `INSERT INTO thread_history_summary(thread_id) VALUES($1)`, id)
		execute(t, db, `INSERT INTO thread_entity(thread_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,state,entity_index) VALUES($1,'epoch','item','item',1,1,false,'{}',1)`, id)
		execute(t, db, `INSERT INTO thread_evidence(thread_id,projection_epoch,entity_cursor,observation_cursor) VALUES($1,'epoch',1,1)`, id)
		execute(t, db, `INSERT INTO thread_native_link(thread_id,projection_epoch,entity_cursor,observation_cursor,source_sequence) VALUES($1,'epoch',1,1,1)`, id)
		execute(t, db, `INSERT INTO thread_payload_manifest(thread_id,projection_epoch,owner_cursor,owner_id,field,generation,revision_cursor,chunk_count,content_bytes) VALUES($1,'epoch',1,'item','text',1,1,1,7)`, id)
		execute(t, db, `INSERT INTO thread_payload_chunk(thread_id,projection_epoch,owner_cursor,owner_id,field,generation,chunk_index,text) VALUES($1,'epoch',1,'item','text',1,0,'"payload"')`, id)
	}

	// Re-run only the new migration after inserting the pre-cutover rows.
	execute(t, db, `DELETE FROM history_service_goose_version WHERE version_id=19`)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	for _, id := range []uuid.UUID{missingUID, emptyUID} {
		if count(t, db, `SELECT count(*) FROM session_history WHERE id=$1`, id) != 0 {
			t.Errorf("session %s without a Sandbox UID remains", id)
		}
		for _, table := range []string{
			"session_event", "session_event_settlement", "session_event_settled_range", "session_event_removed_range",
			"session_event_block", "session_projection_lease", "session_fold_pending", "thread_checkpoint",
			"thread_history_summary", "thread_entity", "thread_evidence", "thread_native_link",
			"thread_payload_manifest", "thread_payload_chunk",
		} {
			column := "session_id"
			if strings.HasPrefix(table, "thread_") {
				column = "thread_id"
			}
			if got := count(t, db, `SELECT count(*) FROM `+table+` WHERE `+column+`=$1`, id); got != 0 {
				t.Errorf("%s rows for %s without a Sandbox UID = %d, want 0", table, id, got)
			}
		}
	}
	if count(t, db, `SELECT count(*) FROM session_history WHERE id=$1 AND sandbox_uid='sandbox-uid'`, retained) != 1 {
		t.Fatal("session with a Sandbox UID was deleted")
	}
}

func stringPointer(value string) *string { return &value }
