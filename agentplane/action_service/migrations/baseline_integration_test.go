package migrations_test

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

	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(migrations.Up, func(database *postgres.Database) int {
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

func TestFreshBaselineAcceptsTypedRows(t *testing.T) {
	db := cloneDatabase(t)
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
}

func seedCompletedSix(t *testing.T, db *sql.DB) {
	t.Helper()
	exec(t, db, `DELETE FROM action_service_goose_version WHERE version_id=7`)
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
	if err := migrations.Up(t.Context(), db); err != nil {
		t.Fatal("adopt version 6:", err)
	}
	if err := migrations.Up(t.Context(), db); err != nil {
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
	if got := count(t, db, `SELECT count(*) FROM action_request r JOIN action_decision d ON d.request_id=r.id
        JOIN action_event e ON e.request_id=r.id WHERE r.arguments='{"human":"untouched"}'::jsonb
        AND r.description='human description' AND d.decision_note='operator kept this note'
        AND d.policy_evidence_proto='\x0801'::bytea AND e.actor_proto='\x0a020801'::bytea`); got != 1 {
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
			err := migrations.Up(t.Context(), db)
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
	exec(t, db, `DELETE FROM action_service_goose_version WHERE version_id=7`)
	if err := migrations.Up(t.Context(), db); err == nil || !strings.Contains(err.Error(), "without completed version 6 history") {
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
			errors <- migrations.Up(t.Context(), db)
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
	if after := relfilenodes(t, db); !reflect.DeepEqual(after, beforeNodes) {
		t.Fatalf("concurrent adoption rewrote an Action table: before %v, after %v", beforeNodes, after)
	}
}
