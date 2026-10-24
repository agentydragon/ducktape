package migrations

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var testDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(runIntegrationTests(m))
}

func runIntegrationTests(m *testing.M) int {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	database, err := postgres.Start(ctx, func(context.Context, *sql.DB) error { return nil })
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	testDatabase = database
	code := m.Run()
	if err := database.Close(); err != nil {
		fmt.Fprintln(os.Stderr, "close PostgreSQL fixture:", err)
		if code == 0 {
			code = 1
		}
	}
	return code
}

func TestUpFreshDatabaseCreatesCurrentSchema(t *testing.T) {
	db := openDatabase(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	assertCurrentSchema(t, db)
	assertAlembicHead(t, db)
	assertGooseVersion(t, db)

	_, err := db.ExecContext(t.Context(), `
		INSERT INTO thread (id, created_at)
		VALUES ('00000000-0000-0000-0000-000000000001', '2026-01-01T00:00:00Z')
	`)
	if err != nil {
		t.Fatal(err)
	}
	var archived, autoTitle, userManaged bool
	var titleStatus string
	var cursor int64
	var revision int
	if err := db.QueryRowContext(t.Context(), `
		SELECT archived, auto_title_on_first_message, title_user_managed, title_status, title_scan_cursor, title_revision
		  FROM thread
		 WHERE id = '00000000-0000-0000-0000-000000000001'
	`).Scan(&archived, &autoTitle, &userManaged, &titleStatus, &cursor, &revision); err != nil {
		t.Fatal(err)
	}
	if archived || autoTitle || userManaged || titleStatus != "waiting" || cursor != 0 || revision != 0 {
		t.Fatalf("unexpected current thread defaults: archived=%v autoTitle=%v userManaged=%v status=%q cursor=%d revision=%d",
			archived, autoTitle, userManaged, titleStatus, cursor, revision)
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatalf("second Up should be a no-op: %v", err)
	}
	assertGooseVersion(t, db)
}

func TestUpAdoptsPopulatedAlembicHeadWithoutChangingRows(t *testing.T) {
	db := openDatabase(t)
	createAlembicHeadSchema(t, db)
	seedCurrentSchema(t, db)
	beforeThreads := jsonRows(t, db, "thread")
	beforeSessions := jsonRows(t, db, "operator_browser_session")

	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if got := jsonRows(t, db, "thread"); got != beforeThreads {
		t.Fatalf("thread rows changed during adoption:\nbefore: %s\nafter:  %s", beforeThreads, got)
	}
	if got := jsonRows(t, db, "operator_browser_session"); got != beforeSessions {
		t.Fatalf("operator session rows changed during adoption:\nbefore: %s\nafter:  %s", beforeSessions, got)
	}
	assertCurrentSchema(t, db)
	assertAlembicHead(t, db)
	assertGooseVersion(t, db)
}

func TestUpRejectsUnknownAndPartialSchemas(t *testing.T) {
	t.Run("unknown Alembic revision", func(t *testing.T) {
		db := openDatabase(t)
		exec(t, db, `
			CREATE TABLE alembic_version_app (version_num varchar(32) NOT NULL PRIMARY KEY);
			INSERT INTO alembic_version_app VALUES ('0035_event_log_into_thread');
			CREATE TABLE thread (id uuid PRIMARY KEY);
		`)
		if err := Up(t.Context(), db); err == nil {
			t.Fatal("Up accepted an unknown Alembic revision")
		}
		assertNotCreated(t, db, "agentplane_app_goose_version")
		assertNotCreated(t, db, "operator_browser_session")
		if got := stringValue(t, db, "SELECT version_num FROM alembic_version_app"); got != "0035_event_log_into_thread" {
			t.Fatalf("Alembic revision changed on rejection: %q", got)
		}
	})

	t.Run("unmarked partial app schema", func(t *testing.T) {
		db := openDatabase(t)
		exec(t, db, `CREATE TABLE thread (id uuid PRIMARY KEY)`)
		if err := Up(t.Context(), db); err == nil {
			t.Fatal("Up accepted a partial unmarked app schema")
		}
		assertNotCreated(t, db, "agentplane_app_goose_version")
		assertNotCreated(t, db, "operator_browser_session")
		assertNotCreated(t, db, "alembic_version_app")
	})

	t.Run("head marker with missing current table", func(t *testing.T) {
		db := openDatabase(t)
		exec(t, db, `
			CREATE TABLE alembic_version_app (version_num varchar(32) NOT NULL PRIMARY KEY);
			INSERT INTO alembic_version_app VALUES ('0036_auto_thread_titles');
			CREATE TABLE thread (id uuid PRIMARY KEY);
		`)
		if err := Up(t.Context(), db); err == nil {
			t.Fatal("Up adopted an incomplete schema at Alembic head")
		}
		assertNotCreated(t, db, "agentplane_app_goose_version")
		assertNotCreated(t, db, "operator_browser_session")
		if got := stringValue(t, db, "SELECT version_num FROM alembic_version_app"); got != "0036_auto_thread_titles" {
			t.Fatalf("Alembic revision changed on rejection: %q", got)
		}
	})

	t.Run("head marker with composite thread key", func(t *testing.T) {
		db := openDatabase(t)
		createAlembicHeadSchema(t, db)
		exec(t, db, `
			ALTER TABLE thread DROP CONSTRAINT thread_pkey;
			ALTER TABLE thread ADD PRIMARY KEY (id, created_at);
		`)
		if err := Up(t.Context(), db); err == nil {
			t.Fatal("Up adopted a thread table with an incompatible primary key")
		}
		assertNotCreated(t, db, "agentplane_app_goose_version")
	})

	t.Run("head marker with altered session check", func(t *testing.T) {
		db := openDatabase(t)
		createAlembicHeadSchema(t, db)
		exec(t, db, `
			ALTER TABLE operator_browser_session DROP CONSTRAINT operator_browser_session_operator_whole;
			ALTER TABLE operator_browser_session ADD CONSTRAINT operator_browser_session_operator_whole
				CHECK (operator_issuer IS NULL);
		`)
		if err := Up(t.Context(), db); err == nil {
			t.Fatal("Up adopted an altered operator session check")
		}
		assertNotCreated(t, db, "agentplane_app_goose_version")
	})

	t.Run("head marker with unexpected foreign key", func(t *testing.T) {
		db := openDatabase(t)
		createAlembicHeadSchema(t, db)
		exec(t, db, `ALTER TABLE thread ADD CONSTRAINT unexpected_thread_fk FOREIGN KEY (id) REFERENCES thread(id)`)
		if err := Up(t.Context(), db); err == nil {
			t.Fatal("Up adopted a thread table with an unexpected foreign key")
		}
		assertNotCreated(t, db, "agentplane_app_goose_version")
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
	assertCurrentSchema(t, db)
	assertAlembicHead(t, db)
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

func assertCurrentSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	assertColumns(t, db, "thread",
		"id", "name", "archived", "created_at", "auto_title_on_first_message", "title_user_managed",
		"title_status", "title_scan_cursor", "title_claimed_at", "title_revision")
	assertColumns(t, db, "operator_browser_session",
		"id", "expires_at", "payload", "absolute_expires_at", "operator_issuer", "operator_subject",
		"operator_username", "access_token", "access_token_expires_at", "refresh_token")

	var primaryKeys int
	if err := db.QueryRowContext(t.Context(), `
		SELECT count(*) FROM pg_constraint
		 WHERE contype = 'p' AND conrelid IN ('thread'::regclass, 'operator_browser_session'::regclass)
	`).Scan(&primaryKeys); err != nil {
		t.Fatal(err)
	}
	if primaryKeys != 2 {
		t.Fatalf("expected both current primary keys, got %d", primaryKeys)
	}
	var checks int
	if err := db.QueryRowContext(t.Context(), `
		SELECT count(*) FROM pg_constraint
		 WHERE contype = 'c'
		   AND conrelid = 'operator_browser_session'::regclass
		   AND conname IN (
		     'operator_browser_session_operator_whole',
		     'operator_browser_session_access_token_whole',
		     'operator_browser_session_tokens_logged_in',
		     'operator_browser_session_refresh_token_with_access_token'
		   )
	`).Scan(&checks); err != nil {
		t.Fatal(err)
	}
	if checks != 4 {
		t.Fatalf("expected four operator session checks, got %d", checks)
	}
	var indexExists bool
	if err := db.QueryRowContext(t.Context(), `
		SELECT EXISTS (
			SELECT 1 FROM pg_indexes
			 WHERE schemaname = current_schema()
			   AND tablename = 'operator_browser_session'
			   AND indexname = 'ix_operator_browser_session_expires_at'
		)
	`).Scan(&indexExists); err != nil {
		t.Fatal(err)
	}
	if !indexExists {
		t.Fatal("missing operator_browser_session expiry index")
	}
	if got := stringValue(t, db, "SELECT column_default FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='thread' AND column_name='title_status'"); got != "'waiting'::text" {
		t.Fatalf("unexpected title_status default: %q", got)
	}
}

func assertColumns(t *testing.T, db *sql.DB, table string, expected ...string) {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `
		SELECT column_name FROM information_schema.columns
		 WHERE table_schema = current_schema() AND table_name = $1
		 ORDER BY ordinal_position
	`, table)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatal(err)
		}
		got = append(got, name)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, ",") != strings.Join(expected, ",") {
		t.Fatalf("%s columns = %v, want %v", table, got, expected)
	}
}

func assertAlembicHead(t *testing.T, db *sql.DB) {
	t.Helper()
	if got := stringValue(t, db, "SELECT version_num FROM alembic_version_app"); got != "0036_auto_thread_titles" {
		t.Fatalf("alembic_version_app = %q, want 0036_auto_thread_titles", got)
	}
}

func assertGooseVersion(t *testing.T, db *sql.DB) {
	t.Helper()
	var applied int
	if err := db.QueryRowContext(t.Context(), `
		SELECT count(*) FROM agentplane_app_goose_version WHERE version_id = 1 AND is_applied
	`).Scan(&applied); err != nil {
		t.Fatal(err)
	}
	if applied != 1 {
		t.Fatalf("expected Goose baseline version 1 applied once, got %d rows", applied)
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

func stringValue(t *testing.T, db *sql.DB, query string) string {
	t.Helper()
	var value string
	if err := db.QueryRowContext(t.Context(), query).Scan(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func jsonRows(t *testing.T, db *sql.DB, table string) string {
	t.Helper()
	query := fmt.Sprintf("SELECT COALESCE(jsonb_agg(to_jsonb(row_value) ORDER BY row_value.id::text)::text, '[]') FROM %s AS row_value", table)
	return stringValue(t, db, query)
}

func exec(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query); err != nil {
		t.Fatal(err)
	}
}

func createAlembicHeadSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	exec(t, db, `
		CREATE TABLE thread (
			id uuid PRIMARY KEY,
			name text,
			archived boolean NOT NULL DEFAULT false,
			created_at timestamptz NOT NULL,
			auto_title_on_first_message boolean NOT NULL DEFAULT false,
			title_user_managed boolean NOT NULL DEFAULT false,
			title_status text NOT NULL DEFAULT 'waiting',
			title_scan_cursor bigint NOT NULL DEFAULT 0,
			title_claimed_at timestamptz,
			title_revision integer NOT NULL DEFAULT 0
		);
		CREATE TABLE operator_browser_session (
			id text PRIMARY KEY,
			expires_at timestamptz NOT NULL,
			payload jsonb NOT NULL,
			absolute_expires_at timestamptz NOT NULL,
			operator_issuer text,
			operator_subject text,
			operator_username text,
			access_token text,
			access_token_expires_at timestamptz,
			refresh_token text,
			CONSTRAINT operator_browser_session_operator_whole CHECK (
				(operator_issuer IS NULL) = (operator_subject IS NULL)
				AND (operator_issuer IS NULL) = (operator_username IS NULL)
			),
			CONSTRAINT operator_browser_session_access_token_whole CHECK (
				(access_token IS NULL) = (access_token_expires_at IS NULL)
			),
			CONSTRAINT operator_browser_session_tokens_logged_in CHECK (
				access_token IS NULL OR operator_issuer IS NOT NULL
			),
			CONSTRAINT operator_browser_session_refresh_token_with_access_token CHECK (
				refresh_token IS NULL OR access_token IS NOT NULL
			)
		);
		CREATE INDEX ix_operator_browser_session_expires_at ON operator_browser_session (expires_at);
		CREATE TABLE alembic_version_app (version_num varchar(32) NOT NULL PRIMARY KEY);
		INSERT INTO alembic_version_app VALUES ('0036_auto_thread_titles');
	`)
}

func seedCurrentSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	exec(t, db, `
		INSERT INTO thread (
			id, created_at, name, archived, auto_title_on_first_message, title_user_managed,
			title_status, title_scan_cursor, title_claimed_at, title_revision
		) VALUES
			('00000000-0000-0000-0000-000000000001', '2026-01-01T00:00:00Z', NULL, false, false, false,
			 'waiting', 0, NULL, 0),
			('00000000-0000-0000-0000-000000000002', '2026-01-02T00:00:00Z', 'A manual title', true, true, true,
			 'done', 42, '2026-01-03T00:00:00Z', 7);
		INSERT INTO operator_browser_session (
			id, expires_at, payload, absolute_expires_at, operator_issuer, operator_subject,
			operator_username, access_token, access_token_expires_at, refresh_token
		) VALUES
			('pending-session', '2027-01-01T00:00:00Z', '{"state":"pending"}', '2027-01-02T00:00:00Z',
			 NULL, NULL, NULL, NULL, NULL, NULL),
			('logged-in-session', '2027-02-01T00:00:00Z', '{"csrf":"kept"}', '2027-02-02T00:00:00Z',
			 'https://issuer.example', 'subject-1', 'Operator', 'secret-access',
			 '2027-02-01T12:00:00Z', 'secret-refresh');
	`)
}
