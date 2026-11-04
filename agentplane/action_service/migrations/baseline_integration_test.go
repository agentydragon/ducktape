package migrations

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
	"github.com/pressly/goose/v3"
)

// The previous release's actual migrations are fixtures, never registered by the service.
//
//go:embed testdata/legacy/*.sql
var previousScripts embed.FS
var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(func(ctx context.Context, db *sql.DB) error {
		files, err := fs.Sub(previousScripts, "testdata/legacy")
		if err != nil {
			return err
		}
		p, err := goose.NewProvider(goose.DialectPostgres, db, files, goose.WithTableName("action_service_goose_version"), goose.WithDisableGlobalRegistry(true))
		if err != nil {
			return err
		}
		_, err = p.Up(ctx)
		return err
	}, func(database *postgres.Database) int {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		if err := database.PrepareCloneTemplate(ctx); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		integrationDatabase = database
		return m.Run()
	}))
}

func cloneDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func exec(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query); err != nil {
		t.Fatal(err)
	}
}

func count(t *testing.T, db *sql.DB, query string) int {
	t.Helper()
	var n int
	if err := db.QueryRowContext(t.Context(), query).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func seed(t *testing.T, db *sql.DB) {
	t.Helper()
	exec(t, db, `INSERT INTO action_request (id, idempotency_key, arguments, state, version, created_at, updated_at, title, caller_namespace, caller_name, action_group, action_name) VALUES ('00000000-0000-0000-0000-000000000001', 'key', '{"human":true}', 'denied', 1, now(), now(), 'keep', 'ns', 'name', 'group', 'action')`)
}

func TestFreshBaseline(t *testing.T) {
	db := cloneDatabase(t)
	exec(t, db, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	seed(t, db)
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=10 AND is_applied`); got != 1 {
		t.Fatalf("baseline rows = %d", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_request WHERE arguments='{"human":true}'::jsonb`); got != 1 {
		t.Fatalf("request rows = %d", got)
	}
}

func TestFreshSchemaMatchesPreviousRelease(t *testing.T) {
	old := cloneDatabase(t)
	fresh := cloneDatabase(t)
	exec(t, fresh, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	if err := Up(t.Context(), fresh); err != nil {
		t.Fatal(err)
	}
	const query = `SELECT coalesce(string_agg(entry, E'\n' ORDER BY entry), '') FROM (
 SELECT 'column:' || table_name || ':' || column_name || ':' || data_type || ':' || is_nullable || ':' || coalesce(column_default, '') AS entry
 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name <> 'action_service_goose_version'
 UNION ALL
 SELECT 'constraint:' || c.relname || ':' || con.conname || ':' || pg_get_constraintdef(con.oid)
 FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid WHERE c.relnamespace=current_schema()::regnamespace AND c.relname <> 'action_service_goose_version'
 UNION ALL
 SELECT 'index:' || indexname || ':' || indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename <> 'action_service_goose_version'
 ) definitions`
	var oldSchema, newSchema string
	if err := old.QueryRowContext(t.Context(), query).Scan(&oldSchema); err != nil {
		t.Fatal(err)
	}
	if err := fresh.QueryRowContext(t.Context(), query).Scan(&newSchema); err != nil {
		t.Fatal(err)
	}
	if oldSchema != newSchema {
		t.Fatalf("fresh schema differs from previous release:\nold:\n%s\nnew:\n%s", oldSchema, newSchema)
	}
}

func TestAdoptCompletedNinePreservesRowsAndHistory(t *testing.T) {
	db := cloneDatabase(t)
	seed(t, db)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id IN (7,8,9,10) AND is_applied`); got != 4 {
		t.Fatalf("history rows = %d", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_request WHERE title='keep' AND arguments='{"human":true}'::jsonb`); got != 1 {
		t.Fatalf("preserved rows = %d", got)
	}
}

func TestRejectIncompleteHistoryAndSchema(t *testing.T) {
	for _, tc := range []struct{ name, alter string }{
		{"version eight", `DELETE FROM action_service_goose_version WHERE version_id=9`},
		{"history gap", `DELETE FROM action_service_goose_version WHERE version_id=8`},
		{"unapplied older migration", `INSERT INTO action_service_goose_version(version_id,is_applied) VALUES (1,false)`},
		{"missing column", `ALTER TABLE action_request DROP COLUMN caller_uid`},
		{"missing table", `DROP TABLE mcp_linkage_flow`},
		{"missing index", `DROP INDEX ix_action_request_state_created`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := cloneDatabase(t)
			seed(t, db)
			exec(t, db, tc.alter)
			err := Up(t.Context(), db)
			if err == nil || !strings.Contains(err.Error(), "Action") {
				t.Fatalf("unexpected migration error: %v", err)
			}
			if count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=10 AND is_applied`) != 0 {
				t.Fatal("recorded rejected baseline")
			}
			if count(t, db, `SELECT count(*) FROM action_request WHERE title='keep'`) != 1 {
				t.Fatal("lost retained request")
			}
		})
	}
}

func TestConcurrentAdoption(t *testing.T) {
	db := cloneDatabase(t)
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() { defer wg.Done(); errs <- Up(t.Context(), db) }()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=10 AND is_applied`); got != 1 {
		t.Fatalf("baseline rows = %d", got)
	}
}
