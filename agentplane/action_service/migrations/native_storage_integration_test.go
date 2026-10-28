package migrations

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/proto"
)

var migrationDatabase *postgres.Database

// The test fixture stops at the last landed schema; Up below is the production cutover.
func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(upToFive, func(database *postgres.Database) int {
		migrationDatabase = database
		return m.Run()
	}))
}

func upToFive(ctx context.Context, db *sql.DB) error {
	files, err := fs.Sub(scripts, "actions")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("action_service_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.UpTo(ctx, 5)
	return err
}

func v5Database(tb testing.TB) *sql.DB {
	tb.Helper()
	db, err := sql.Open("postgres", migrationDatabase.Clone(tb))
	if err != nil {
		tb.Fatal(err)
	}
	tb.Cleanup(func() { _ = db.Close() })
	return db
}

func execSQL(tb testing.TB, db *sql.DB, query string, args ...any) {
	tb.Helper()
	if _, err := db.ExecContext(tb.Context(), query, args...); err != nil {
		tb.Fatal(err)
	}
}

func countSQL(tb testing.TB, db *sql.DB, query string, args ...any) int {
	tb.Helper()
	var count int
	if err := db.QueryRowContext(tb.Context(), query, args...).Scan(&count); err != nil {
		tb.Fatal(err)
	}
	return count
}

const insertRequest = `INSERT INTO action_request
	(id, idempotency_key, action, arguments, state, version, created_at, updated_at,
	 title, description, caller_namespace, caller_name)
	VALUES ($1, $2, $3::jsonb, $4::jsonb, 'denied', 2, $5, $5, $6, $7, 'migration-ns', 'migration-caller')`

const insertEvent = `INSERT INTO action_event (request_id, sequence, state, at, actor)
	VALUES ($1, $2, 'denied', $3, $4::jsonb)`

const insertDecision = `INSERT INTO action_decision
	(id, request_id, verdict, provider, decision_note, idempotency_key, decided_at,
	 reason_code, reason_description, policy_evidence, operator_issuer, operator_subject)
	VALUES ($1, $2, 'deny', 'migration-provider', $3, $4, $5, 'reason', 'human explanation', $6::jsonb,
	 'https://issuer.example', 'operator-subject')`

const evidenceJSON = `{"bindings":[{"namespace":"n","name":"b","resource_version":"rv7"}],"policy_sets":[{"namespace":"n","name":"set","generation":4}],"matched":{"namespace":"n","policy_set":"set","source":"rules","index":5,"type":"exact_actions"}}`

func seedLegacy(tb testing.TB, db *sql.DB, suffix int, actor, evidence any) string {
	tb.Helper()
	id := fmt.Sprintf("00000000-0000-0000-0000-%012d", suffix)
	decisionID := fmt.Sprintf("10000000-0000-0000-0000-%012d", suffix)
	at := time.Date(2024, 7, 8, 9, 10, 11, 0, time.UTC)
	execSQL(tb, db, insertRequest, id, fmt.Sprintf("key-%d", suffix), `{"group":"legacy-group","name":"legacy-name"}`,
		`{"human":[true,null,{"n":3}]}`, at, "human title", "human description")
	execSQL(tb, db, insertEvent, id, 2, at, actor)
	execSQL(tb, db, insertDecision, decisionID, id, "operator kept this note", fmt.Sprintf("decision-%d", suffix), at, evidence)
	return id
}

func TestNativeStoragePreservesLegacyRows(t *testing.T) {
	db := v5Database(t)
	id := seedLegacy(t, db, 1, `{"account":{"namespace":"actor-ns","name":"actor-name"}}`, evidenceJSON)
	seedLegacy(t, db, 2, `{"issuer":"https://issuer.example","subject":"human"}`, `null`)
	seedLegacy(t, db, 3, nil, nil)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatalf("repeated migration: %v", err)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id = 6 AND is_applied`); got != 1 {
		t.Fatalf("v6 versions = %d", got)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() AND
		(table_name, column_name) IN (('action_request','action'),('action_event','actor'),('action_decision','policy_evidence'))`); got != 0 {
		t.Fatalf("legacy columns left = %d", got)
	}
	var group, name, title, description, callerNS, callerName, state string
	var arguments []byte
	var version int
	var created, updated time.Time
	if err := db.QueryRowContext(t.Context(), `SELECT action_group, action_name, arguments, state, version, created_at,
		updated_at, title, description, caller_namespace, caller_name FROM action_request WHERE id=$1`, id).
		Scan(&group, &name, &arguments, &state, &version, &created, &updated, &title, &description, &callerNS, &callerName); err != nil {
		t.Fatal(err)
	}
	var gotArguments, wantArguments any
	if err := json.Unmarshal(arguments, &gotArguments); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(`{"human":[true,null,{"n":3}]}`), &wantArguments); err != nil {
		t.Fatal(err)
	}
	if group != "legacy-group" || name != "legacy-name" || !reflect.DeepEqual(gotArguments, wantArguments) ||
		state != "denied" || version != 2 || !created.Equal(updated) || title != "human title" ||
		description != "human description" || callerNS != "migration-ns" || callerName != "migration-caller" {
		t.Errorf("request data changed: %s/%s %s %s v%d %s %s %s %s/%s", group, name, arguments, state,
			version, created, title, description, callerNS, callerName)
	}
	var actorBytes, evidenceBytes []byte
	if err := db.QueryRowContext(t.Context(), `SELECT actor_proto FROM action_event WHERE request_id=$1 AND sequence=2`, id).Scan(&actorBytes); err != nil {
		t.Fatal(err)
	}
	actor := new(action_servicepb.Principal)
	if err := proto.Unmarshal(actorBytes, actor); err != nil {
		t.Fatal(err)
	}
	if actor.GetCaller().GetNamespace() != "actor-ns" || actor.GetCaller().GetName() != "actor-name" {
		t.Errorf("caller actor = %v", actor)
	}
	var note, operatorIssuer, operatorSubject, reasonDescription string
	if err := db.QueryRowContext(t.Context(), `SELECT policy_evidence_proto, decision_note, operator_issuer, operator_subject,
		reason_description FROM action_decision WHERE request_id=$1`, id).
		Scan(&evidenceBytes, &note, &operatorIssuer, &operatorSubject, &reasonDescription); err != nil {
		t.Fatal(err)
	}
	evidence := new(action_servicepb.PolicyEvidence)
	if err := proto.Unmarshal(evidenceBytes, evidence); err != nil {
		t.Fatal(err)
	}
	wantEvidence := &action_servicepb.PolicyEvidence{
		Bindings:   []*action_servicepb.BindingEvidence{{Namespace: "n", Name: "b", ResourceVersion: "rv7"}},
		PolicySets: []*action_servicepb.PolicySetEvidence{{Namespace: "n", Name: "set", Generation: 4}},
		Matched: &action_servicepb.MatchedPolicy{
			Namespace: "n", PolicySet: "set", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
			Index: 5, Type: action_servicepb.PolicyKind_POLICY_KIND_EXACT_ACTIONS,
		},
	}
	if !proto.Equal(evidence, wantEvidence) || note != "operator kept this note" ||
		operatorIssuer != "https://issuer.example" || operatorSubject != "operator-subject" || reasonDescription != "human explanation" {
		t.Errorf("decision data changed: evidence=%v want=%v note=%q operator=%q/%q reason=%q",
			evidence, wantEvidence, note, operatorIssuer, operatorSubject, reasonDescription)
	}
	var operatorBytes []byte
	if err := db.QueryRowContext(t.Context(), `SELECT actor_proto FROM action_event WHERE request_id=$1 AND sequence=2`,
		"00000000-0000-0000-0000-000000000002").Scan(&operatorBytes); err != nil {
		t.Fatal(err)
	}
	operator := new(action_servicepb.Principal)
	if err := proto.Unmarshal(operatorBytes, operator); err != nil {
		t.Fatal(err)
	}
	if operator.GetOperator().GetIssuer() != "https://issuer.example" || operator.GetOperator().GetSubject() != "human" {
		t.Errorf("operator actor = %v", operator)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_event WHERE actor_proto IS NULL`); got != 1 {
		t.Errorf("SQL NULL actor count = %d", got)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_decision WHERE policy_evidence_proto IS NULL`); got != 2 {
		t.Errorf("SQL and JSON null evidence count = %d", got)
	}
}

func TestInvalidLegacyRowRollsBackWholeCutover(t *testing.T) {
	db := v5Database(t)
	seedLegacy(t, db, 1, `{"account":{"namespace":"n","name":"a"}}`, evidenceJSON)
	seedLegacy(t, db, 2, nil, `{"unexpected":"human data must not be discarded"}`)
	if err := Up(t.Context(), db); err == nil {
		t.Fatal("migration accepted unknown evidence")
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id = 6 AND is_applied`); got != 0 {
		t.Errorf("applied v6 after failure: %d", got)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM information_schema.columns WHERE table_name='action_request' AND column_name='action'`); got != 1 {
		t.Errorf("legacy schema rolled forward after failure: %d", got)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_request WHERE action = '{"group":"legacy-group","name":"legacy-name"}'::jsonb`); got != 2 {
		t.Errorf("legacy rows changed after failure: %d", got)
	}
	execSQL(t, db, `UPDATE action_decision SET policy_evidence=$1::jsonb WHERE id='10000000-0000-0000-0000-000000000002'`, evidenceJSON)
	if err := Up(t.Context(), db); err != nil {
		t.Fatalf("retry after repair: %v", err)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_decision WHERE policy_evidence_proto IS NOT NULL`); got != 2 {
		t.Errorf("converted evidence after repair: %d", got)
	}
}

func TestEveryHistoricalPolicyKindConverts(t *testing.T) {
	for _, test := range []struct {
		name string
		kind action_servicepb.PolicyKind
	}{
		{"exact_actions", action_servicepb.PolicyKind_POLICY_KIND_EXACT_ACTIONS},
		{"argument_schema", action_servicepb.PolicyKind_POLICY_KIND_ARGUMENT_SCHEMA},
		{"github_repository", action_servicepb.PolicyKind_POLICY_KIND_GITHUB_REPOSITORY},
		{"github_public_repository", action_servicepb.PolicyKind_POLICY_KIND_GITHUB_PUBLIC_REPOSITORY},
		{"home_assistant_entity_control", action_servicepb.PolicyKind_POLICY_KIND_HOME_ASSISTANT_ENTITY_CONTROL},
	} {
		t.Run(test.name, func(t *testing.T) {
			db := v5Database(t)
			evidence := strings.Replace(evidenceJSON, `"type":"exact_actions"`, `"type":"`+test.name+`"`, 1)
			var repository *action_servicepb.MatchedRepository
			if test.name == "github_repository" || test.name == "github_public_repository" {
				confirmedPublic := test.name == "github_public_repository"
				evidence = strings.Replace(evidence, `"type":"`+test.name+`"}`,
					fmt.Sprintf(`"type":"%s","repository":{"owner":"human-owner","repository":"human-repo","confirmed_public":%t}}`,
						test.name, confirmedPublic), 1)
				repository = &action_servicepb.MatchedRepository{
					Owner: "human-owner", Repository: "human-repo", ConfirmedPublic: confirmedPublic,
				}
			}
			id := seedLegacy(t, db, 1, nil, evidence)
			if err := Up(t.Context(), db); err != nil {
				t.Fatal(err)
			}
			var payload []byte
			if err := db.QueryRowContext(t.Context(), `SELECT policy_evidence_proto FROM action_decision WHERE request_id=$1`, id).
				Scan(&payload); err != nil {
				t.Fatal(err)
			}
			var decoded action_servicepb.PolicyEvidence
			if err := proto.Unmarshal(payload, &decoded); err != nil {
				t.Fatal(err)
			}
			want := &action_servicepb.PolicyEvidence{
				Bindings:   []*action_servicepb.BindingEvidence{{Namespace: "n", Name: "b", ResourceVersion: "rv7"}},
				PolicySets: []*action_servicepb.PolicySetEvidence{{Namespace: "n", Name: "set", Generation: 4}},
				Matched: &action_servicepb.MatchedPolicy{
					Namespace: "n", PolicySet: "set", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
					Index: 5, Type: test.kind, Repository: repository,
				},
			}
			if !proto.Equal(&decoded, want) {
				t.Errorf("evidence = %v, want %v", &decoded, want)
			}
		})
	}
}

func TestConcurrentCutoversSerialize(t *testing.T) {
	db := v5Database(t)
	seedLegacy(t, db, 1, nil, nil)
	var wg sync.WaitGroup
	errors := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errors <- Up(t.Context(), db)
		}()
	}
	wg.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Errorf("concurrent migration: %v", err)
		}
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=6 AND is_applied`); got != 1 {
		t.Errorf("applied v6 count = %d", got)
	}
}

func TestCancellationDuringConversionRollsBack(t *testing.T) {
	db := v5Database(t)
	seedLegacy(t, db, 1, nil, evidenceJSON)
	const lockID = 4472006
	// The trigger parks the UPDATE after expansion and decoding have begun. Its advisory
	// lock is held by another connection, which never touches the tables under migration.
	execSQL(t, db, `CREATE FUNCTION wait_for_native_storage_test() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN PERFORM pg_advisory_xact_lock(4472006); RETURN NEW; END $$`)
	execSQL(t, db, `CREATE TRIGGER wait_for_native_storage_test BEFORE UPDATE ON action_request
		FOR EACH ROW EXECUTE FUNCTION wait_for_native_storage_test()`)
	locker, err := db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer locker.Close()
	if _, err := locker.ExecContext(t.Context(), `SELECT pg_advisory_lock($1)`, lockID); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	result := make(chan error, 1)
	go func() { result <- Up(ctx, db) }()
	deadline := time.NewTimer(30 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(10 * time.Millisecond)
	defer tick.Stop()
	for countSQL(t, db, `SELECT count(*) FROM pg_stat_activity
		WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%UPDATE action_request AS request%'`) == 0 {
		select {
		case err := <-result:
			t.Fatalf("migration finished before conversion gate: %v", err)
		case <-deadline.C:
			t.Fatal("migration never reached conversion gate")
		case <-tick.C:
		}
	}
	cancel()
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("cancelled migration succeeded")
		}
	case <-deadline.C:
		t.Fatal("cancelled migration did not return")
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=6 AND is_applied`); got != 0 {
		t.Errorf("applied v6 after cancellation: %d", got)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM information_schema.columns WHERE table_name='action_request' AND column_name='action'`); got != 1 {
		t.Errorf("legacy schema changed after cancellation: %d", got)
	}
	if err := locker.Close(); err != nil {
		t.Fatal(err)
	}
	execSQL(t, db, `DROP TRIGGER wait_for_native_storage_test ON action_request`)
	execSQL(t, db, `DROP FUNCTION wait_for_native_storage_test()`)
	if err := Up(t.Context(), db); err != nil {
		t.Fatalf("retry after cancellation: %v", err)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_service_goose_version WHERE version_id=6 AND is_applied`); got != 1 {
		t.Errorf("applied v6 after retry: %d", got)
	}
	if got := countSQL(t, db, `SELECT count(*) FROM action_decision WHERE policy_evidence_proto IS NOT NULL`); got != 1 {
		t.Errorf("converted evidence after retry: %d", got)
	}
}

// BenchmarkNativeStorageCutover measures only the outage transaction over a populated v5 schema.
// The fixture and seed are outside the timed section; each iteration gets a fresh clone.
func BenchmarkNativeStorageCutover(b *testing.B) {
	for i := 0; i < b.N; i++ {
		b.StopTimer()
		db := v5Database(b)
		evidenceBytes := seedBenchmark(b, db)
		var before, after string
		if err := db.QueryRowContext(b.Context(), `SELECT pg_current_wal_insert_lsn()`).Scan(&before); err != nil {
			b.Fatal(err)
		}
		b.StartTimer()
		err := Up(b.Context(), db)
		b.StopTimer()
		if err != nil {
			b.Fatal(err)
		}
		if err := db.QueryRowContext(b.Context(), `SELECT pg_current_wal_insert_lsn()`).Scan(&after); err != nil {
			b.Fatal(err)
		}
		var walBytes int64
		if err := db.QueryRowContext(b.Context(), `SELECT pg_wal_lsn_diff($1::pg_lsn,$2::pg_lsn)::bigint`, after, before).Scan(&walBytes); err != nil {
			b.Fatal(err)
		}
		b.ReportMetric(float64(walBytes), "wal_bytes/op")
		b.ReportMetric(float64(evidenceBytes), "legacy_evidence_bytes/op")
	}
}

func seedBenchmark(b *testing.B, db *sql.DB) int {
	b.Helper()
	const requests = 10_000
	const eventsPerRequest = 5
	tx, err := db.BeginTx(b.Context(), nil)
	if err != nil {
		b.Fatal(err)
	}
	defer tx.Rollback()
	request, err := tx.PrepareContext(b.Context(), insertRequest)
	if err != nil {
		b.Fatal(err)
	}
	defer request.Close()
	event, err := tx.PrepareContext(b.Context(), insertEvent)
	if err != nil {
		b.Fatal(err)
	}
	defer event.Close()
	decision, err := tx.PrepareContext(b.Context(), insertDecision)
	if err != nil {
		b.Fatal(err)
	}
	defer decision.Close()
	at := time.Date(2024, 7, 8, 9, 10, 11, 0, time.UTC)
	// More than 20 MB of logical JSON evidence, above the observed live database's 10.5 MB.
	benchmarkEvidence := strings.Replace(evidenceJSON, "rv7", strings.Repeat("e", 2200), 1)
	for i := 1; i <= requests; i++ {
		id := fmt.Sprintf("00000000-0000-0000-0000-%012d", i)
		decisionID := fmt.Sprintf("10000000-0000-0000-0000-%012d", i)
		if _, err := request.ExecContext(b.Context(), id, fmt.Sprintf("key-%d", i),
			`{"group":"legacy-group","name":"legacy-name"}`, `{"human":{"nested":[1,2,3],"text":"preserved"}}`,
			at, "human title", "human description"); err != nil {
			b.Fatal(err)
		}
		for sequence := 1; sequence <= eventsPerRequest; sequence++ {
			actor := any(nil)
			if sequence%2 == 0 {
				actor = `{"account":{"namespace":"actor-ns","name":"actor-name"}}`
			}
			if _, err := event.ExecContext(b.Context(), id, sequence, at, actor); err != nil {
				b.Fatal(err)
			}
		}
		if _, err := decision.ExecContext(b.Context(), decisionID, id, "operator kept this note",
			fmt.Sprintf("decision-%d", i), at, benchmarkEvidence); err != nil {
			b.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		b.Fatal(err)
	}
	return requests * len(benchmarkEvidence)
}
