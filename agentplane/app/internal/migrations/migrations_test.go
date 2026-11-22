package migrations

import (
	"context"
	"database/sql"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var testDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(func(context.Context, *sql.DB) error { return nil }, func(database *postgres.Database) int {
		testDatabase = database
		return m.Run()
	}))
}

func TestUpFreshDatabaseCreatesCurrentSchema(t *testing.T) {
	db := openDatabase(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	assertGooseVersion(t, db)

	_, err := db.ExecContext(t.Context(), `
		INSERT INTO thread (id, created_at)
		VALUES ('00000000-0000-0000-0000-000000000001', '2026-01-01T00:00:00Z')
	`)
	if err != nil {
		t.Fatal(err)
	}
	var archived bool
	if err := db.QueryRowContext(t.Context(), `
		SELECT archived
		  FROM thread
		 WHERE id = '00000000-0000-0000-0000-000000000001'
	`).Scan(&archived); err != nil {
		t.Fatal(err)
	}
	if archived {
		t.Fatalf("unexpected current thread defaults: archived=%v", archived)
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatalf("second Up should be a no-op: %v", err)
	}
	assertGooseVersion(t, db)
}

func TestUpRejectsUnknownAndPartialSchemas(t *testing.T) {
	t.Run("unmarked partial app schema", func(t *testing.T) {
		db := openDatabase(t)
		exec(t, db, `CREATE TABLE thread (id uuid PRIMARY KEY)`)
		if err := Up(t.Context(), db); err == nil {
			t.Fatal("Up accepted a partial unmarked app schema")
		}
		assertNoAppliedMigration(t, db)
		assertNotCreated(t, db, "operator_browser_session")
	})
}

func TestConcurrentUpRunnersSerialize(t *testing.T) {
	db := openDatabase(t)
	start := make(chan struct{})
	errs := make(chan error, 2)
	var runners sync.WaitGroup
	runners.Add(2)
	for range 2 {
		go func() {
			defer runners.Done()
			<-start
			ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
			defer cancel()
			errs <- Up(ctx, db)
		}()
	}
	close(start)
	runners.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent Up failed: %v", err)
		}
	}
	assertGooseVersion(t, db)
}

func openDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", testDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(8)
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Error(err)
		}
	})
	if err := db.PingContext(t.Context()); err != nil {
		t.Fatal(err)
	}
	return db
}

func assertGooseVersion(t *testing.T, db *sql.DB) {
	t.Helper()
	var applied int
	if err := db.QueryRowContext(t.Context(), `
		SELECT count(*) FROM agentplane_app_goose_version WHERE version_id IN (3, 4) AND is_applied
	`).Scan(&applied); err != nil {
		t.Fatal(err)
	}
	if applied != 2 {
		t.Fatalf("expected Goose versions 3 and 4 applied once, got %d rows", applied)
	}
}

func assertNoAppliedMigration(t *testing.T, db *sql.DB) {
	t.Helper()
	var applied int
	if err := db.QueryRowContext(t.Context(), `
		SELECT count(*) FROM agentplane_app_goose_version WHERE version_id > 0 AND is_applied
	`).Scan(&applied); err != nil {
		t.Fatal(err)
	}
	if applied != 0 {
		t.Fatalf("rejected schema recorded %d applied migrations", applied)
	}
}

func assertNotCreated(t *testing.T, db *sql.DB, table string) {
	t.Helper()
	var exists bool
	if err := db.QueryRowContext(t.Context(), "SELECT to_regclass(current_schema() || '.' || $1) IS NOT NULL", table).Scan(&exists); err != nil {
		t.Fatal(err)
	}
	if exists {
		t.Fatalf("unexpected table %s exists", table)
	}
}

func exec(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query); err != nil {
		t.Fatal(err)
	}
}
