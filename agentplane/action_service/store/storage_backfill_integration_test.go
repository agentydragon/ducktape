package store

import (
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

func TestBackfillActionStoragePreservesRowsAndResumesAfterFailure(t *testing.T) {
	f := newFixture(t)
	const rowCount = 514
	if _, err := f.db.ExecContext(t.Context(), `
		INSERT INTO action_request (id, idempotency_key, action, arguments, state, version, created_at, updated_at,
			title, description, caller_namespace, caller_name)
		SELECT ('00000000-0000-0000-0000-' || lpad(to_hex(n), 12, '0'))::uuid,
			'legacy-' || n, '{"group":"legacy-group","name":"legacy-action"}'::jsonb, '{}'::jsonb,
			'denied', 2, now(), now(), 'legacy request', NULL, 'legacy-caller', 'owner'
		FROM generate_series(1, $1) AS n`, rowCount); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.ExecContext(t.Context(), `
		INSERT INTO action_event (request_id, sequence, state, at, actor)
		SELECT ('00000000-0000-0000-0000-' || lpad(to_hex(n), 12, '0'))::uuid, 1, 'denied', now(),
			CASE n % 4
				WHEN 0 THEN jsonb_build_object('issuer', 'https://issuer.test', 'subject', 'legacy-operator')
				WHEN 1 THEN jsonb_build_object('account', jsonb_build_object('namespace', 'legacy-ns', 'name', 'legacy-caller'))
			WHEN 2 THEN 'null'::jsonb
			ELSE NULL::jsonb
			END
		FROM generate_series(1, $1) AS n`, rowCount); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.ExecContext(t.Context(), `
		INSERT INTO action_decision (id, request_id, verdict, provider, idempotency_key, decided_at, policy_evidence)
		SELECT ('00000000-0000-0000-0000-' || lpad(to_hex(n), 12, '0'))::uuid,
			('00000000-0000-0000-0000-' || lpad(to_hex(n), 12, '0'))::uuid, 'deny', 'legacy-provider',
			'legacy-' || n, now(),
			CASE
				WHEN n = $1 THEN jsonb_build_object('bindings', '[]'::jsonb, 'policy_sets', '[]'::jsonb,
					'matched', jsonb_build_object('namespace', 'legacy-policy', 'policy_set', 'legacy-set',
						'source', 'rules', 'index', 1, 'type', 'unknown-policy'))
				WHEN n % 4 = 1 THEN jsonb_build_object(
					'bindings', jsonb_build_array(jsonb_build_object('namespace', 'legacy-binding-ns',
						'name', 'legacy-binding', 'resource_version', '17')),
					'policy_sets', jsonb_build_array(jsonb_build_object('namespace', 'legacy-set-ns',
						'name', 'legacy-set', 'generation', 23)),
					'matched', jsonb_build_object('namespace', 'legacy-policy', 'policy_set', 'legacy-set',
						'source', 'rules', 'index', 1, 'type', 'github_repository',
						'repository', jsonb_build_object('owner', 'legacy-owner', 'repository', 'legacy-repo',
							'confirmed_public', true)))
				WHEN n % 4 = 2 THEN 'null'::jsonb
				WHEN n % 4 = 3 THEN jsonb_build_object(
					'bindings', jsonb_build_array(jsonb_build_object('namespace', 'legacy-binding-ns',
						'name', 'legacy-binding', 'resource_version', '17')),
					'policy_sets', jsonb_build_array(jsonb_build_object('namespace', 'legacy-set-ns',
						'name', 'legacy-set', 'generation', 23)),
					'matched', jsonb_build_object('namespace', 'legacy-policy', 'policy_set', 'legacy-set',
						'source', 'rules', 'index', 1, 'type', 'github_repository',
						'repository', jsonb_build_object('owner', 'legacy-owner', 'repository', 'legacy-repo',
							'confirmed_public', true)))
				ELSE NULL::jsonb
			END
		FROM generate_series(1, $1) AS n`, rowCount); err != nil {
		t.Fatal(err)
	}
	// Keep the EXPLAIN representative of a batch touching a small part of a populated table.
	if _, err := f.db.ExecContext(t.Context(), `
		INSERT INTO action_request (id, idempotency_key, action, arguments, state, version, created_at, updated_at,
		title, description, caller_namespace, caller_name, action_group, action_name)
		SELECT ('10000000-0000-0000-0000-' || lpad(to_hex(n), 12, '0'))::uuid,
			'plan-' || n, '{"group":"already","name":"ready"}'::jsonb,
			jsonb_build_object('payload', repeat('x', 2048)),
			'denied', 2, now(), now(), repeat('p', 2048), repeat('d', 512), 'plan-only', 'owner', 'already', 'ready'
		FROM generate_series(1, 20000) AS n`); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.ExecContext(t.Context(), "ANALYZE action_request"); err != nil {
		t.Fatal(err)
	}

	assertActionStoragePlans(t, f)
	if err := BackfillActionStorage(t.Context(), f.db); err == nil || !strings.Contains(err.Error(), "unknown policy kind") {
		t.Fatalf("BackfillActionStorage error = %v, want malformed policy evidence", err)
	}
	if got := f.count(t, `SELECT count(*) FROM action_request WHERE action_group = 'legacy-group' AND action_name = 'legacy-action'`); got != rowCount {
		t.Fatalf("typed request identities = %d rows, want %d after first pass", got, rowCount)
	}
	if got := f.count(t, `SELECT count(*) FROM action_event WHERE actor_proto IS NOT NULL`); got == 0 || got >= rowCount {
		t.Fatalf("converted actors = %d rows, want some present actors and SQL NULL for absent actors", got)
	}
	if got := f.count(t, `SELECT count(*) FROM action_decision WHERE id <= '00000000-0000-0000-0000-000000000200'::uuid
		AND policy_evidence IS NOT NULL AND policy_evidence <> 'null'::jsonb AND policy_evidence_proto IS NOT NULL`); got != 256 {
		t.Fatalf("first policy-evidence batch has %d converted present rows, want 256", got)
	}
	if got := f.count(t, `SELECT count(*) FROM action_decision WHERE id > '00000000-0000-0000-0000-000000000200'::uuid AND policy_evidence_proto IS NOT NULL`); got != 0 {
		t.Fatalf("failed policy-evidence batch committed %d rows, want none", got)
	}
	if got := f.count(t, `SELECT count(*) FROM action_event WHERE actor IS NOT NULL AND actor <> 'null'::jsonb AND actor_proto IS NULL`); got != 0 {
		t.Fatalf("present legacy actors still lack bytes after first pass: %d", got)
	}
	if got := f.count(t, `SELECT count(*) FROM action_decision WHERE id IN (
		'00000000-0000-0000-0000-000000000002'::uuid,
		'00000000-0000-0000-0000-000000000004'::uuid) AND policy_evidence_proto IS NULL`); got != 2 {
		t.Fatalf("absent SQL NULL/JSON null policy evidence lost null behavior: got %d absent rows", got)
	}

	malformedID := sequentialUUID(rowCount)
	f.exec(t, `UPDATE action_decision SET policy_evidence = jsonb_build_object('bindings', '[]'::jsonb,
		'policy_sets', '[]'::jsonb, 'matched', jsonb_build_object('namespace', 'legacy-policy',
		'policy_set', 'legacy-set', 'source', 'rules', 'index', 1, 'type', 'exact_actions')) WHERE id = $1`, malformedID)
	if err := BackfillActionStorage(t.Context(), f.db); err != nil {
		t.Fatalf("resume BackfillActionStorage: %v", err)
	}
	if err := BackfillActionStorage(t.Context(), f.db); err != nil {
		t.Fatalf("repeat BackfillActionStorage: %v", err)
	}
	if got := f.count(t, `SELECT count(*) FROM action_request WHERE caller_namespace = 'legacy-caller'`); got != rowCount {
		t.Fatalf("legacy action_request rows = %d after backfill, want %d", got, rowCount)
	}
	if got := f.count(t, `SELECT count(*) FROM action_event`); got != rowCount {
		t.Fatalf("action_event rows = %d after backfill, want %d", got, rowCount)
	}
	if got := f.count(t, `SELECT count(*) FROM action_decision`); got != rowCount {
		t.Fatalf("action_decision rows = %d after backfill, want %d", got, rowCount)
	}
	if got := f.count(t, `SELECT count(*) FROM action_decision WHERE policy_evidence_proto IS NOT NULL`); got != 258 {
		t.Fatalf("present policy evidence rows = %d, want 258", got)
	}

	var actorBytes, evidenceBytes []byte
	operatorEventID := sequentialUUID(4)
	if err := f.db.QueryRowContext(t.Context(), `SELECT actor_proto FROM action_event WHERE request_id = $1`, operatorEventID).Scan(&actorBytes); err != nil {
		t.Fatal(err)
	}
	var actor action_servicepb.Principal
	if err := proto.Unmarshal(actorBytes, &actor); err != nil {
		t.Fatal(err)
	}
	wantActor := &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{
		Operator: &action_servicepb.Operator{Issuer: "https://issuer.test", Subject: "legacy-operator"},
	}}
	if !proto.Equal(&actor, wantActor) {
		t.Fatalf("backfilled actor = %v, want %v", &actor, wantActor)
	}

	decisionID := sequentialUUID(3)
	if err := f.db.QueryRowContext(t.Context(), `SELECT policy_evidence_proto FROM action_decision WHERE id = $1`, decisionID).Scan(&evidenceBytes); err != nil {
		t.Fatal(err)
	}
	var evidence action_servicepb.PolicyEvidence
	if err := proto.Unmarshal(evidenceBytes, &evidence); err != nil {
		t.Fatal(err)
	}
	wantEvidence := &action_servicepb.PolicyEvidence{
		Bindings:   []*action_servicepb.BindingEvidence{{Namespace: "legacy-binding-ns", Name: "legacy-binding", ResourceVersion: "17"}},
		PolicySets: []*action_servicepb.PolicySetEvidence{{Namespace: "legacy-set-ns", Name: "legacy-set", Generation: 23}},
		Matched: &action_servicepb.MatchedPolicy{
			Namespace: "legacy-policy", PolicySet: "legacy-set", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
			Index: 1, Type: action_servicepb.PolicyKind_POLICY_KIND_GITHUB_REPOSITORY,
			Repository: &action_servicepb.MatchedRepository{Owner: "legacy-owner", Repository: "legacy-repo", ConfirmedPublic: true},
		},
	}
	if !proto.Equal(&evidence, wantEvidence) {
		t.Fatalf("backfilled policy evidence = %v, want %v", &evidence, wantEvidence)
	}

	request := f.get(t, sequentialUUID(3))
	wantHistoricalEvidence := &PolicyEvidence{
		Bindings:   []BindingEvidence{{Namespace: "legacy-binding-ns", Name: "legacy-binding", ResourceVersion: "17"}},
		PolicySets: []PolicySetEvidence{{Namespace: "legacy-set-ns", Name: "legacy-set", Generation: 23}},
		Matched: MatchedPolicy{
			Namespace: "legacy-policy", PolicySet: "legacy-set", Source: PolicySourceRules, Index: 1,
			Type:       PolicyGitHubRepository,
			Repository: &MatchedRepository{Owner: "legacy-owner", Repository: "legacy-repo", ConfirmedPublic: true},
		},
	}
	if request.Action != (ActionIdentity{Group: "legacy-group", Name: "legacy-action"}) || request.Decision == nil ||
		!reflect.DeepEqual(request.Decision.PolicyEvidence, wantHistoricalEvidence) {
		t.Fatalf("store read after backfill changed historical request content: %+v", request)
	}
	events := f.events(t, sequentialUUID(4))
	if len(events) != 1 || events[0].Actor.GetOperator().GetSubject() != "legacy-operator" {
		t.Fatalf("store read after backfill changed historical actor: %+v", events)
	}
}

func assertActionStoragePlans(t *testing.T, f fixture) {
	t.Helper()
	for _, test := range []struct {
		name  string
		query string
		args  []any
	}{
		{"request keyset", `EXPLAIN (COSTS OFF) SELECT id, action_group, action_name,
			CASE WHEN action_group IS NULL OR action_name IS NULL THEN action::text END
			FROM action_request ORDER BY id LIMIT $1 FOR UPDATE`,
			[]any{actionStorageBackfillBatchSize}},
		{"request cursor", `EXPLAIN (COSTS OFF) SELECT id, action_group, action_name,
			CASE WHEN action_group IS NULL OR action_name IS NULL THEN action::text END
			FROM action_request WHERE id > $1::uuid ORDER BY id LIMIT $2 FOR UPDATE`,
			[]any{sequentialUUID(actionStorageBackfillBatchSize), actionStorageBackfillBatchSize}},
		{"request unnest update", `EXPLAIN (COSTS OFF) UPDATE action_request AS request
			SET action_group = batch.action_group, action_name = batch.action_name
			FROM unnest($1::uuid[], $2::text[], $3::text[]) AS batch(id, action_group, action_name)
			WHERE request.id = batch.id AND (request.action_group IS NULL OR request.action_name IS NULL)`,
			[]any{pq.Array([]string{sequentialUUID(1).String(), sequentialUUID(2).String()}),
				pq.Array([]string{"legacy-group", "legacy-group"}), pq.Array([]string{"legacy-action", "legacy-action"})}},
	} {
		rows, err := f.db.QueryContext(t.Context(), test.query, test.args...)
		if err != nil {
			t.Fatalf("EXPLAIN %s: %v", test.name, err)
		}
		var lines []string
		for rows.Next() {
			var line string
			if err := rows.Scan(&line); err != nil {
				_ = rows.Close()
				t.Fatalf("scan EXPLAIN %s: %v", test.name, err)
			}
			lines = append(lines, line)
		}
		if err := rows.Close(); err != nil {
			t.Fatalf("close EXPLAIN %s: %v", test.name, err)
		}
		plan := strings.Join(lines, "\n")
		if strings.Contains(plan, "Sort") || strings.Contains(plan, "Seq Scan on action_request") {
			t.Fatalf("EXPLAIN %s contains an avoidable full-table sort/scan:\n%s", test.name, plan)
		}
		if (test.name == "request keyset" || test.name == "request cursor") && !strings.Contains(plan, "action_request_pkey") {
			t.Fatalf("EXPLAIN %s does not use the native primary key:\n%s", test.name, plan)
		}
	}
}

func sequentialUUID(n int) uuid.UUID {
	return uuid.MustParse(fmt.Sprintf("00000000-0000-0000-0000-%012x", n))
}
