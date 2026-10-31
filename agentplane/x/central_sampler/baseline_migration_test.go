package centralsampler

import (
	"context"
	"database/sql"
	"io/fs"
	"reflect"
	"strings"
	"sync"
	"testing"
	"testing/fstest"
	"time"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

func migrationExec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func migrationCount(t *testing.T, db *sql.DB, query string, args ...any) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(t.Context(), query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func seedCompletedNativeSeven(t *testing.T, db *sql.DB) {
	t.Helper()
	migrationExec(t, db, `ALTER TABLE sampler_conversation DROP COLUMN code_host_instance`)
	migrationExec(t, db, `DELETE FROM central_sampler_goose_version WHERE version_id>=8`)
	migrationExec(t, db, `INSERT INTO central_sampler_goose_version(version_id,is_applied)
		SELECT version_id,true FROM generate_series(1,7) AS version_id`)
}

func migrateBaselineEight(ctx context.Context, db *sql.DB) error {
	contents, err := fs.ReadFile(migrationFiles, "schema/0008_final_schema.sql")
	if err != nil {
		return err
	}
	files := fstest.MapFS{"0008_final_schema.sql": &fstest.MapFile{Data: contents}}
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(7241821))
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("central_sampler_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

func samplerRelationNodes(t *testing.T, db *sql.DB) map[string]int64 {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT relname,relfilenode FROM pg_class
		WHERE relnamespace=current_schema()::regnamespace AND relkind='r'
		AND relname LIKE 'sampler_%' ORDER BY relname`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	nodes := make(map[string]int64)
	for rows.Next() {
		var name string
		var node int64
		if err := rows.Scan(&name, &node); err != nil {
			t.Fatal(err)
		}
		nodes[name] = node
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return nodes
}

func TestFreshNativeBaseline(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
		WHERE version_id=8 AND is_applied`); got != 1 {
		t.Fatalf("fresh baseline version rows = %d, want 1", got)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_conversation
		WHERE id=$1 AND api='responses'`, session.ConversationID); got != 1 {
		t.Fatalf("native conversation rows = %d, want 1", got)
	}
}

func TestNativeBaselineAdoptsSevenAndPreservesRows(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	migrationExec(t, store.DB(), `INSERT INTO sampler_conversation_item(conversation_id,ordinal,record)
		VALUES($1,1,'{"type":"reasoning","provider_id":9007199254740993}'::jsonb)`, session.ConversationID)
	seedCompletedNativeSeven(t, store.DB())
	beforeNodes := samplerRelationNodes(t, store.DB())
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	if err := migrateBaselineEight(ctx, store.DB()); err != nil {
		t.Fatalf("adopt native version 7: %v", err)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
		WHERE version_id BETWEEN 1 AND 7 AND is_applied`); got != 7 {
		t.Fatalf("retained version rows = %d, want 7", got)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
		WHERE version_id=8 AND is_applied`); got != 1 {
		t.Fatalf("adoption version rows = %d, want 1", got)
	}
	if after := samplerRelationNodes(t, store.DB()); !reflect.DeepEqual(beforeNodes, after) {
		t.Fatalf("adoption rewrote sampler tables: before %v, after %v", beforeNodes, after)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM information_schema.columns
		WHERE table_schema=current_schema() AND table_name='sampler_conversation'
		AND column_name='code_host_instance' AND data_type='uuid' AND is_nullable='YES'`); got != 1 {
		t.Fatalf("adopted nullable host marker columns = %d, want 1", got)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_conversation_item
		WHERE conversation_id=$1 AND record->>'provider_id'='9007199254740993'`, session.ConversationID); got != 1 {
		t.Fatalf("retained provider record rows = %d, want 1", got)
	}
	if err := migrateBaselineEight(t.Context(), store.DB()); err != nil {
		t.Fatalf("repeat baseline initialization: %v", err)
	}
}

func TestNativeBaselineRejectsIncompleteSevenWithoutMarkingAdopted(t *testing.T) {
	for _, tc := range []struct{ name, change string }{
		{"older history", `DELETE FROM central_sampler_goose_version WHERE version_id=7`},
		{"nullable API", `ALTER TABLE sampler_conversation ALTER COLUMN api DROP NOT NULL`},
		{"missing native check", `ALTER TABLE sampler_conversation DROP CONSTRAINT sampler_conversation_api_check`},
		{"missing native index", `DROP INDEX sampler_process_model_session_idx`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newTestStore(t)
			seedCompletedNativeSeven(t, store.DB())
			migrationExec(t, store.DB(), tc.change)
			beforeNodes := samplerRelationNodes(t, store.DB())
			err := migrateBaselineEight(t.Context(), store.DB())
			if err == nil || !strings.Contains(err.Error(), "Sampler") {
				t.Fatalf("Migrate error = %v, want incomplete native schema rejection", err)
			}
			if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
				WHERE version_id=8 AND is_applied`); got != 0 {
				t.Fatalf("rejected version 8 recorded %d rows", got)
			}
			if after := samplerRelationNodes(t, store.DB()); !reflect.DeepEqual(beforeNodes, after) {
				t.Fatalf("rejection rewrote sampler tables: before %v, after %v", beforeNodes, after)
			}
		})
	}
}

func TestNativeBaselineRejectsUnmarkedExistingSchema(t *testing.T) {
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id>=8`)
	if err := migrateBaselineEight(t.Context(), store.DB()); err == nil || !strings.Contains(err.Error(), "without completed version 7 history") {
		t.Fatalf("Migrate error = %v, want unmarked schema rejection", err)
	}
}

func TestNativeBaselineConcurrentAdoption(t *testing.T) {
	store := newTestStore(t)
	seedCompletedNativeSeven(t, store.DB())
	beforeNodes := samplerRelationNodes(t, store.DB())
	var workers sync.WaitGroup
	errors := make(chan error, 2)
	for range 2 {
		workers.Add(1)
		go func() {
			defer workers.Done()
			errors <- migrateBaselineEight(t.Context(), store.DB())
		}()
	}
	workers.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal("concurrent adoption:", err)
		}
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
		WHERE version_id=8 AND is_applied`); got != 1 {
		t.Fatalf("concurrent adoption version rows = %d, want 1", got)
	}
	if after := samplerRelationNodes(t, store.DB()); !reflect.DeepEqual(beforeNodes, after) {
		t.Fatalf("concurrent adoption rewrote sampler tables: before %v, after %v", beforeNodes, after)
	}
}

func TestBaselineProviderStartsAfterAdoptingHostMarker(t *testing.T) {
	store := newTestStore(t)
	seedCompletedNativeSeven(t, store.DB())
	if err := migrateBaselineEight(t.Context(), store.DB()); err != nil {
		t.Fatalf("baseline cannot adopt nullable host marker: %v", err)
	}
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Read(t.Context(), owner); err != nil {
		t.Fatalf("conversation read changed with host marker: %v", err)
	}
}

func TestSnapshotCheckpointMigrationReconcilesLegacyMarkersOnce(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const target = "history:8080"
	if err := store.CommitCheckpoint(t.Context(), session.ID, target, session.Cursor); err != nil {
		t.Fatal(err)
	}
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=9`)
	if err := Migrate(t.Context(), store.DB()); err != nil {
		t.Fatal(err)
	}
	checkpoint, err := store.Checkpoint(t.Context(), session.ID, target)
	if err != nil || checkpoint != nil {
		t.Fatalf("legacy checkpoint=%v err=%v, want pending metadata reconciliation", checkpoint, err)
	}
	pending, err := store.PendingSessionIDs(t.Context(), target, "", 16)
	if err != nil || len(pending) != 1 || pending[0] != session.ID {
		t.Fatalf("pending=%v err=%v, want original Session", pending, err)
	}
	entries, err := store.Events(t.Context(), session.ID, 0, 16)
	if err != nil || len(entries) != int(session.Cursor) {
		t.Fatalf("retained entries=%v err=%v", entries, err)
	}
	if err := store.CommitCheckpoint(t.Context(), session.ID, target, session.Cursor); err != nil {
		t.Fatal(err)
	}
	if err := Migrate(t.Context(), store.DB()); err != nil {
		t.Fatal(err)
	}
	checkpoint, err = store.Checkpoint(t.Context(), session.ID, target)
	if err != nil || checkpoint == nil || *checkpoint != session.Cursor {
		t.Fatalf("completed snapshot checkpoint=%v err=%v, want retained on restart", checkpoint, err)
	}
}
