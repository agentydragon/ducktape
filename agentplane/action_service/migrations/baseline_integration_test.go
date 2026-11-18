package migrations

import (
	"context"
	"database/sql"
	"fmt"
	"io/fs"
	"os"
	"path"
	"testing"
	"testing/fstest"
	"time"

	postgresmigrate "github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
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
	return postgres.Open(t, integrationDatabase.Clone(t))
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

func TestDelegationAttributionMigrationAndOlderWriterCompatibility(t *testing.T) {
	db := cloneDatabase(t)
	exec(t, db, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	priorSchema := schemaThroughVersion(t, 11)
	priorProvider, err := priorSchema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := priorProvider.Up(t.Context()); err != nil {
		t.Fatal(err)
	}
	seed(t, db)

	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	var key, title, namespace, name, state string
	var argumentsMatch bool
	var actor []byte
	var sessionID, connectionID, grantID sql.NullString
	if err := db.QueryRowContext(t.Context(), `
		SELECT idempotency_key, arguments = '{"human":true}'::jsonb, title, caller_namespace, caller_name, state,
			delegation_actor_proto, provenance_session_id::text, provenance_connection_id::text,
			provenance_connection_grant_id::text
		FROM action_request WHERE id = '00000000-0000-0000-0000-000000000001'`).
		Scan(&key, &argumentsMatch, &title, &namespace, &name, &state, &actor, &sessionID, &connectionID, &grantID); err != nil {
		t.Fatal(err)
	}
	if key != "key" || !argumentsMatch || title != "keep" || namespace != "ns" || name != "name" || state != "denied" {
		t.Fatalf("existing receipt changed during migration: key=%q arguments_match=%t title=%q caller=%s/%s state=%q",
			key, argumentsMatch, title, namespace, name, state)
	}
	if actor != nil || sessionID.Valid || connectionID.Valid || grantID.Valid {
		t.Fatalf("existing receipt attribution was inferred: actor=%x session=%v connection=%v grant=%v",
			actor, sessionID, connectionID, grantID)
	}

	olderProvider, err := priorSchema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := olderProvider.Up(t.Context()); err != nil {
		t.Fatalf("prior version could not start against the additive schema: %v", err)
	}
	exec(t, db, `INSERT INTO action_request (id, idempotency_key, arguments, state, version, created_at, updated_at,
		title, caller_namespace, caller_name, action_group, action_name)
		VALUES ('00000000-0000-0000-0000-000000000002', 'old-writer', '{}', 'decision_pending', 1, now(), now(),
			'old writer', 'ns', 'old-writer', 'group', 'action')`)
	if got := count(t, db, `SELECT count(*) FROM action_request WHERE id = '00000000-0000-0000-0000-000000000002'
		AND delegation_actor_proto IS NULL AND provenance_session_id IS NULL
		AND provenance_connection_id IS NULL AND provenance_connection_grant_id IS NULL`); got != 1 {
		t.Fatalf("old writer row attribution NULL count = %d, want 1", got)
	}
}

func schemaThroughVersion(t *testing.T, version int) postgresmigrate.Schema {
	t.Helper()
	entries, err := fs.ReadDir(scripts, "actions")
	if err != nil {
		t.Fatal(err)
	}
	files := fstest.MapFS{}
	for _, entry := range entries {
		if entry.Name() >= fmt.Sprintf("%05d_", version+1) {
			continue
		}
		name := path.Join("actions", entry.Name())
		content, err := fs.ReadFile(scripts, name)
		if err != nil {
			t.Fatal(err)
		}
		files[name] = &fstest.MapFile{Data: content, Mode: 0o644}
	}
	return postgresmigrate.Schema{Files: files, Dir: "actions", Table: schema.Table, LockID: schema.LockID}
}

func TestExecutionHeartbeatMigrationPreservesExecutionsAndExecutorLiveness(t *testing.T) {
	db := cloneDatabase(t)
	exec(t, db, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	priorProvider, err := schemaThroughVersion(t, 12).Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := priorProvider.Up(t.Context()); err != nil {
		t.Fatal(err)
	}
	seed(t, db)
	exec(t, db, `INSERT INTO action_execution (id, request_id, state, created_at, heartbeat_at)
		VALUES ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001',
			'succeeded', now(), now())`)
	exec(t, db, `INSERT INTO action_executor_heartbeat (executor_id, started_at, heartbeat_at)
		VALUES ('test-executor', now(), now())`)

	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if got := count(t, db, `SELECT count(*) FROM action_execution
		WHERE id = '00000000-0000-0000-0000-000000000002'
		AND request_id = '00000000-0000-0000-0000-000000000001' AND state = 'succeeded'`); got != 1 {
		t.Fatalf("preserved execution rows = %d, want 1", got)
	}
	exec(t, db, `UPDATE action_executor_heartbeat SET heartbeat_at = heartbeat_at + interval '1 minute'
		WHERE executor_id = 'test-executor'`)
	if got := count(t, db, `SELECT count(*) FROM action_executor_heartbeat
		WHERE executor_id = 'test-executor' AND heartbeat_at = started_at + interval '1 minute'`); got != 1 {
		t.Fatalf("live executor heartbeat rows = %d, want 1", got)
	}

	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Down(t.Context()); err != nil {
		t.Fatal(err)
	}
	if _, err := priorProvider.Up(t.Context()); err != nil {
		t.Fatalf("prior image schema could not start after rollback: %v", err)
	}
	var heartbeat sql.NullTime
	if err := db.QueryRowContext(t.Context(), `SELECT heartbeat_at FROM action_execution
		WHERE id = '00000000-0000-0000-0000-000000000002'`).Scan(&heartbeat); err != nil {
		t.Fatal(err)
	}
	if heartbeat.Valid {
		t.Fatalf("rollback restored a discarded heartbeat value: %v", heartbeat.Time)
	}
}
