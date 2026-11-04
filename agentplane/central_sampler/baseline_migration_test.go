package centralsampler

import (
	"database/sql"
	"embed"
	"io/fs"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/pressly/goose/v3"
)

//go:embed testdata/*.sql
var previousMigrations embed.FS

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

func restoreVersion10(t *testing.T, db *sql.DB) {
	t.Helper()
	migrationExec(t, db, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	files, err := fs.Sub(previousMigrations, "testdata")
	if err != nil {
		t.Fatal(err)
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("central_sampler_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 9); err != nil {
		t.Fatal("restore previous SQL migrations:", err)
	}
	// Migration 10 converted stored requests without changing the schema. No requests exist yet.
	migrationExec(t, db, `INSERT INTO central_sampler_goose_version (version_id, is_applied) VALUES (10, true)`)
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

func TestFreshSamplerBaseline(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
		WHERE version_id=11 AND is_applied`); got != 1 {
		t.Fatalf("fresh baseline history rows = %d, want 1", got)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_conversation
		WHERE id=$1 AND api='responses'`, session.ConversationID); got != 1 {
		t.Fatalf("fresh conversation rows = %d, want 1", got)
	}
}

func TestSamplerBaselineAdoptsVersion10WithoutRewritingRows(t *testing.T) {
	store := newTestStore(t)
	restoreVersion10(t, store.DB())
	session := openSessionForStore(t, store, "")
	migrationExec(t, store.DB(), `INSERT INTO sampler_conversation_item(conversation_id,ordinal,record)
		VALUES($1,1,'{"type":"reasoning","provider_id":9007199254740993}'::jsonb)`, session.ConversationID)
	const target = "history:8080"
	if err := store.CommitCheckpoint(t.Context(), session.ID, target, session.Cursor); err != nil {
		t.Fatal(err)
	}
	var configurationBefore []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT configuration FROM sampler_session WHERE id=$1`, session.ID).Scan(&configurationBefore); err != nil {
		t.Fatal(err)
	}
	beforeNodes := samplerRelationNodes(t, store.DB())
	beforeHistory := migrationHistory(t, store.DB())
	if err := Migrate(t.Context(), store.DB()); err != nil {
		t.Fatal("adopt version 10:", err)
	}
	if afterNodes := samplerRelationNodes(t, store.DB()); !reflect.DeepEqual(beforeNodes, afterNodes) {
		t.Fatalf("adoption rewrote tables: before %v, after %v", beforeNodes, afterNodes)
	}
	afterHistory := migrationHistory(t, store.DB())
	if !reflect.DeepEqual(beforeHistory, afterHistory[:len(beforeHistory)]) {
		t.Fatal("adoption rewrote previous Goose history")
	}
	var configurationAfter []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT configuration FROM sampler_session WHERE id=$1`, session.ID).Scan(&configurationAfter); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(configurationBefore, configurationAfter) {
		t.Fatal("adoption changed retained request bytes")
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_conversation_item
		WHERE conversation_id=$1 AND record->>'provider_id'='9007199254740993'`, session.ConversationID); got != 1 {
		t.Fatalf("retained provider records = %d, want 1", got)
	}
	checkpoint, err := store.Checkpoint(t.Context(), session.ID, target)
	if err != nil || checkpoint == nil || *checkpoint != session.Cursor {
		t.Fatalf("retained checkpoint=%v err=%v", checkpoint, err)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=11 AND is_applied`); got != 1 {
		t.Fatalf("adoption history rows = %d, want 1", got)
	}
}

func TestSamplerBaselineMatchesPreviousSchema(t *testing.T) {
	fresh := newTestStore(t)
	previous := newTestStore(t)
	restoreVersion10(t, previous.DB())
	got, want := samplerCatalog(t, fresh.DB()), samplerCatalog(t, previous.DB())
	if !reflect.DeepEqual(got, want) {
		for i := range min(len(got), len(want)) {
			if got[i] != want[i] {
				t.Fatalf("schema differs at %d: baseline %s; previous %s", i, got[i], want[i])
			}
		}
		t.Fatalf("schema object counts: baseline %d, previous %d", len(got), len(want))
	}
}

func samplerCatalog(t *testing.T, db *sql.DB) []string {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT description FROM (
		SELECT 'table ' || table_name AS description FROM information_schema.tables
		WHERE table_schema=current_schema() AND table_name <> 'central_sampler_goose_version'
		UNION ALL
		SELECT 'column ' || table_name || '.' || column_name || ' ' || data_type || ' ' ||
			is_nullable || ' ' || COALESCE(column_default,'') || ' ' || is_identity
		FROM information_schema.columns
		WHERE table_schema=current_schema() AND table_name <> 'central_sampler_goose_version'
		UNION ALL
		SELECT 'constraint ' || c.conrelid::regclass::text || '.' || c.conname || ' ' || pg_get_constraintdef(c.oid)
		FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
		WHERE n.nspname=current_schema() AND c.conrelid::regclass::text <> 'central_sampler_goose_version'
		UNION ALL
		SELECT 'index ' || tablename || '.' || indexname || ' ' || indexdef FROM pg_indexes
		WHERE schemaname=current_schema() AND tablename <> 'central_sampler_goose_version'
	) objects ORDER BY description`)
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
	return result
}

func TestSamplerBaselineRejectsPartialHistoryAndSchema(t *testing.T) {
	for _, tc := range []struct{ name, change string }{
		{"missing migration 9", `DELETE FROM central_sampler_goose_version WHERE version_id=9`},
		{"missing migration 10", `DELETE FROM central_sampler_goose_version WHERE version_id=10`},
		{"missing host marker", `ALTER TABLE sampler_conversation DROP COLUMN code_host_instance`},
		{"missing native index", `DROP INDEX sampler_process_model_session_idx`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newTestStore(t)
			restoreVersion10(t, store.DB())
			migrationExec(t, store.DB(), tc.change)
			before := samplerRelationNodes(t, store.DB())
			err := Migrate(t.Context(), store.DB())
			if err == nil || !strings.Contains(err.Error(), "Sampler") {
				t.Fatalf("Migrate error = %v, want rejected predecessor", err)
			}
			if after := samplerRelationNodes(t, store.DB()); !reflect.DeepEqual(before, after) {
				t.Fatalf("rejection rewrote tables: before %v, after %v", before, after)
			}
			if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=11 AND is_applied`); got != 0 {
				t.Fatalf("rejection marked baseline applied: %d", got)
			}
		})
	}
}

func TestSamplerBaselineAdoptsCompleteOlderHistory(t *testing.T) {
	store := newTestStore(t)
	restoreVersion10(t, store.DB())
	migrationExec(t, store.DB(), `INSERT INTO central_sampler_goose_version (version_id, is_applied)
		SELECT version_id, true FROM generate_series(1,7) version_id`)
	if err := Migrate(t.Context(), store.DB()); err != nil {
		t.Fatal("adopt complete 1–10 history:", err)
	}
}

func TestSamplerBaselineRejectsUnmarkedDatabase(t *testing.T) {
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=11`)
	if err := Migrate(t.Context(), store.DB()); err == nil || !strings.Contains(err.Error(), "unrecognized objects") {
		t.Fatalf("Migrate error = %v, want unmarked schema rejection", err)
	}
}

func TestSamplerBaselineConcurrentFreshCreation(t *testing.T) {
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	var workers sync.WaitGroup
	errors := make(chan error, 2)
	for range 2 {
		workers.Add(1)
		go func() { defer workers.Done(); errors <- Migrate(t.Context(), store.DB()) }()
	}
	workers.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal("concurrent fresh baseline:", err)
		}
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=11 AND is_applied`); got != 1 {
		t.Fatalf("fresh baseline history rows = %d, want 1", got)
	}
}

type samplerGooseRow struct {
	id      int64
	version int64
	applied bool
	stamp   time.Time
}

func migrationHistory(t *testing.T, db *sql.DB) []samplerGooseRow {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT id,version_id,is_applied,tstamp FROM central_sampler_goose_version ORDER BY id`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var history []samplerGooseRow
	for rows.Next() {
		var row samplerGooseRow
		if err := rows.Scan(&row.id, &row.version, &row.applied, &row.stamp); err != nil {
			t.Fatal(err)
		}
		history = append(history, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return history
}
