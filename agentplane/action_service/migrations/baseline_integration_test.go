package migrations

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(Up, func(database *postgres.Database) int {
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
