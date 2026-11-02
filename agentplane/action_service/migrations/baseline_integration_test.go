package migrations

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(func(ctx context.Context, db *sql.DB) error {
		provider, err := newProvider(db)
		if err != nil {
			return err
		}
		_, err = provider.UpTo(ctx, 8)
		return err
	}, func(database *postgres.Database) int {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		if err := database.PrepareCloneTemplate(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "prepare Action schema clone template:", err)
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

func exec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query, args...); err != nil {
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

var retiredTables = []string{
	"external_connection", "external_connection_grant", "connection_enrollment",
	"oauth_registered_client", "oauth_authorization", "oauth_token",
}

func assertLegacyTablesAbsent(t *testing.T, db *sql.DB) {
	t.Helper()
	for _, table := range retiredTables {
		if got := count(t, db, `SELECT count(*) FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = '`+table+`'`); got != 0 {
			t.Errorf("retired Actions table %s still exists", table)
		}
	}
	if got := count(t, db, `SELECT count(*) FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name IN
        ('mcp_oauth_token_state', 'mcp_server_linkage', 'mcp_linkage_flow',
         'action_request', 'action_event', 'action_decision', 'action_push_subscription')`); got != 7 {
		t.Errorf("live Actions tables found = %d, want 7", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=9 AND is_applied`); got != 1 {
		t.Errorf("version 9 history rows = %d, want 1", got)
	}
}

func TestRetiresPopulatedLegacyTablesWithoutChangingActionHistory(t *testing.T) {
	db := cloneDatabase(t) // The template has the genuine version-8 schema.
	exec(t, db, `INSERT INTO external_connection
        (id, display_name, version, created_at, updated_at)
        VALUES ('00000000-0000-0000-0000-000000000010', 'old', 6, now(), now())`)
	for revision := 1; revision <= 3; revision++ {
		status := "revoked"
		if revision == 3 {
			status = "active"
		}
		exec(t, db, `INSERT INTO external_connection_grant
            (id, connection_id, revision, issuer, client_id, status, created_at, original_caller)
            VALUES ($1, '00000000-0000-0000-0000-000000000010', $2,
                    'https://old.example/', 'client', $3, now(), '{"namespace":"test","name":"caller"}')`,
			fmt.Sprintf("00000000-0000-0000-0000-%012d", 20+revision), revision, status)
	}
	for enrollment := 1; enrollment <= 4; enrollment++ {
		exec(t, db, `INSERT INTO connection_enrollment
            (id, handle_hash, issuer, client_id, redirect_uri, code_challenge,
             upstream_url, expires_at, version, connection_id)
            VALUES ($1, $2, 'https://old.example/', 'client', 'https://callback.example/',
                    $2, 'https://upstream.example/', now() - interval '1 day', 1,
                    '00000000-0000-0000-0000-000000000010')`,
			fmt.Sprintf("00000000-0000-0000-0000-%012d", 30+enrollment), fmt.Sprintf("old-%d", enrollment))
	}
	exec(t, db, `INSERT INTO action_request
        (id, idempotency_key, arguments, state, version, created_at, updated_at,
         title, caller_namespace, caller_name, action_group, action_name)
        VALUES ('00000000-0000-0000-0000-000000000040', 'keep', '{}', 'denied', 1,
                now(), now(), 'retain history', 'test', 'caller', 'group', 'action')`)
	exec(t, db, `INSERT INTO mcp_oauth_token_state
	    (id, server_id, access_token, token_type, scope, expires_at, token_revision,
	     updated_at, refresh_failure_count)
	    VALUES ('00000000-0000-0000-0000-000000000041',
	            'upstream-server', 'secret', 'Bearer', '[]', now(), 1, now(), 0)`)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("upgrade populated version-8 schema:", err)
	}
	assertLegacyTablesAbsent(t, db)
	if got := count(t, db, `SELECT count(*) FROM action_request WHERE title = 'retain history'`); got != 1 {
		t.Errorf("retained Action history rows = %d, want 1", got)
	}
	if got := count(t, db, `SELECT count(*) FROM mcp_oauth_token_state WHERE access_token = 'secret'`); got != 1 {
		t.Errorf("retained upstream token-state rows = %d, want 1", got)
	}
}

func TestRetirementRejectsUnexpectedDependencyAtomically(t *testing.T) {
	db := cloneDatabase(t)
	exec(t, db, `CREATE VIEW unexpected_connection_reader AS SELECT id FROM external_connection`)
	if err := Up(t.Context(), db); err == nil {
		t.Fatal("migration unexpectedly dropped a table with an external dependent view")
	}
	if got := count(t, db, `SELECT count(*) FROM information_schema.tables WHERE table_schema = current_schema()
        AND table_name IN ('external_connection', 'external_connection_grant', 'connection_enrollment',
                           'oauth_registered_client', 'oauth_authorization', 'oauth_token')`); got != 6 {
		t.Fatalf("legacy tables after failed migration = %d, want 6", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=9 AND is_applied`); got != 0 {
		t.Fatalf("failed version 9 recorded %d applied rows", got)
	}
}

func TestFreshBaselineAcceptsTypedRows(t *testing.T) {
	db := cloneDatabase(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("fresh migration through version 9:", err)
	}
	exec(t, db, `INSERT INTO action_request
        (id, idempotency_key, arguments, state, version, created_at, updated_at,
         title, description, caller_namespace, caller_name, action_group, action_name)
        VALUES ('00000000-0000-0000-0000-000000000001', 'human-key', '{"human":[1,true]}',
        'denied', 2, now(), now(), 'human title', 'human description', 'ns', 'name', 'group', 'action')`)
	exec(t, db, `INSERT INTO action_event (request_id, sequence, state, at, actor_proto)
        VALUES ('00000000-0000-0000-0000-000000000001', 1, 'denied', now(), '\x0a020801'::bytea)`)
	exec(t, db, `INSERT INTO action_decision
        (id, request_id, verdict, provider, decision_note, idempotency_key, decided_at, policy_evidence_proto)
        VALUES ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001',
        'deny', 'human', 'operator kept this note', 'decision-key', now(), '\x0801'::bytea)`)
	if got := count(t, db, `SELECT count(*) FROM action_request r JOIN action_event e ON e.request_id=r.id
        JOIN action_decision d ON d.request_id=r.id WHERE r.description='human description'
        AND r.arguments='{"human":[1,true]}'::jsonb AND d.decision_note='operator kept this note'
        AND e.actor_proto='\x0a020801'::bytea AND d.policy_evidence_proto='\x0801'::bytea`); got != 1 {
		t.Fatalf("typed request/decision/event joined rows = %d, want 1", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=7 AND is_applied`); got != 1 {
		t.Fatalf("fresh baseline version rows = %d, want 1", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=8 AND is_applied`); got != 1 {
		t.Fatalf("fresh caller UID migration rows = %d, want 1", got)
	}
	assertLegacyTablesAbsent(t, db)
}

func seedCompletedSix(t *testing.T, db *sql.DB) {
	t.Helper()
	exec(t, db, `DELETE FROM action_service_goose_version WHERE version_id IN (7, 8)`)
	exec(t, db, `ALTER TABLE action_request DROP COLUMN caller_uid`)
	exec(t, db, `INSERT INTO action_service_goose_version(version_id,is_applied)
        SELECT version_id,true FROM generate_series(1,6) AS version_id`)
}

func relfilenodes(t *testing.T, db *sql.DB) map[string]uint32 {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT relname, relfilenode FROM pg_class
        WHERE relnamespace=current_schema()::regnamespace AND relkind='r'
        AND relname LIKE 'action_%' AND relname <> 'action_service_goose_version' ORDER BY relname`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	result := make(map[string]uint32)
	for rows.Next() {
		var name string
		var node uint32
		if err := rows.Scan(&name, &node); err != nil {
			t.Fatal(err)
		}
		result[name] = node
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return result
}

func TestAdoptSixPreservesHistoryRowsAndRelations(t *testing.T) {
	db := cloneDatabase(t)
	seedCompletedSix(t, db)
	exec(t, db, `INSERT INTO action_request
        (id, idempotency_key, arguments, state, version, created_at, updated_at,
         title, description, caller_namespace, caller_name, action_group, action_name)
        VALUES ('00000000-0000-0000-0000-000000000002', 'human-key', '{"human":"untouched"}',
        'denied', 2, now(), now(), 'human title', 'human description', 'ns', 'name', 'group', 'action')`)
	exec(t, db, `INSERT INTO action_decision
        (id, request_id, verdict, provider, decision_note, idempotency_key, decided_at, policy_evidence_proto)
        VALUES ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002',
        'deny', 'human', 'operator kept this note', 'decision-key', now(), '\x0801'::bytea)`)
	exec(t, db, `INSERT INTO action_event (request_id, sequence, state, at, actor_proto)
        VALUES ('00000000-0000-0000-0000-000000000002', 1, 'denied', now(), '\x0a020801'::bytea)`)
	beforeNodes := relfilenodes(t, db)
	beforeHistory := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id BETWEEN 1 AND 6 AND is_applied`)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("adopt version 6:", err)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("repeat adoption:", err)
	}
	if after := relfilenodes(t, db); !reflect.DeepEqual(after, beforeNodes) {
		t.Fatalf("adoption rewrote an Action table: before %v, after %v", beforeNodes, after)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id BETWEEN 1 AND 6 AND is_applied`); got != beforeHistory {
		t.Fatalf("prior Goose history changed: %d to %d", beforeHistory, got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=7 AND is_applied`); got != 1 {
		t.Fatalf("version 7 history rows = %d, want 1", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=8 AND is_applied`); got != 1 {
		t.Fatalf("version 8 history rows = %d, want 1", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_request r JOIN action_decision d ON d.request_id=r.id
        JOIN action_event e ON e.request_id=r.id WHERE r.arguments='{"human":"untouched"}'::jsonb
        AND r.description='human description' AND d.decision_note='operator kept this note'
        AND d.policy_evidence_proto='\x0801'::bytea AND e.actor_proto='\x0a020801'::bytea
        AND r.caller_uid IS NULL`); got != 1 {
		t.Fatalf("preserved human and typed history rows = %d, want 1", got)
	}
}

func TestRejectsOlderAndPartialSchemasWithoutChanges(t *testing.T) {
	for _, tc := range []struct{ name, alter string }{
		{"version 5", `DELETE FROM action_service_goose_version WHERE version_id=6`},
		{"missing typed column", `ALTER TABLE action_decision DROP COLUMN policy_evidence_proto`},
		{"missing index", `DROP INDEX ix_action_request_state_created`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := cloneDatabase(t)
			seedCompletedSix(t, db)
			exec(t, db, tc.alter)
			beforeNodes := relfilenodes(t, db)
			err := Up(t.Context(), db)
			if err == nil || !strings.Contains(err.Error(), "Action") {
				t.Fatalf("Up error = %v, want Action schema rejection", err)
			}
			if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=7 AND is_applied`); got != 0 {
				t.Fatalf("rejected version 7 recorded %d rows", got)
			}
			if after := relfilenodes(t, db); !reflect.DeepEqual(after, beforeNodes) {
				t.Fatalf("rejected migration changed table storage: before %v, after %v", beforeNodes, after)
			}
		})
	}
}

func TestRejectsUnmarkedExistingSchema(t *testing.T) {
	db := cloneDatabase(t)
	exec(t, db, `DELETE FROM action_service_goose_version WHERE version_id IN (7, 8)`)
	exec(t, db, `ALTER TABLE action_request DROP COLUMN caller_uid`)
	if err := Up(t.Context(), db); err == nil || !strings.Contains(err.Error(), "without completed version 6 history") {
		t.Fatalf("Up error = %v, want unmarked schema rejection", err)
	}
}

func TestConcurrentAdoption(t *testing.T) {
	db := cloneDatabase(t)
	seedCompletedSix(t, db)
	beforeNodes := relfilenodes(t, db)
	var workers sync.WaitGroup
	errors := make(chan error, 2)
	for range 2 {
		workers.Add(1)
		go func() {
			defer workers.Done()
			errors <- Up(t.Context(), db)
		}()
	}
	workers.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal("concurrent adoption:", err)
		}
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=7 AND is_applied`); got != 1 {
		t.Fatalf("concurrent version 7 history rows = %d, want 1", got)
	}
	if got := count(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=8 AND is_applied`); got != 1 {
		t.Fatalf("concurrent version 8 history rows = %d, want 1", got)
	}
	if after := relfilenodes(t, db); !reflect.DeepEqual(after, beforeNodes) {
		t.Fatalf("concurrent adoption rewrote an Action table: before %v, after %v", beforeNodes, after)
	}
}
