package store

import (
	"testing"

	"github.com/google/uuid"
)

func TestActionStorageValidationRequiresBackfill(t *testing.T) {
	f := newFixture(t)
	id := uuid.New()
	f.exec(t, `ALTER TABLE action_request DROP CONSTRAINT action_request_typed_identity_present`)
	f.exec(t, `INSERT INTO action_request (id, idempotency_key, action, arguments, title, caller_namespace, caller_name,
		state, version, created_at, updated_at)
		VALUES ($1, 'legacy-cutover', '{"group":"legacy-group","name":"legacy-action"}', '{}', 'legacy request',
		'legacy-caller', 'owner', 'denied', 2, now(), now())`, id)
	f.exec(t, `ALTER TABLE action_request ADD CONSTRAINT action_request_typed_identity_present
		CHECK (action_group IS NOT NULL AND action_name IS NOT NULL) NOT VALID`)

	if err := ValidateActionStorageConstraints(t.Context(), f.db); err == nil {
		t.Fatal("validated a database whose historical request lacks its typed identity")
	}
	if f.count(t, `SELECT count(*) FROM pg_constraint WHERE conname = 'action_request_typed_identity_present' AND convalidated`) != 0 {
		t.Fatal("failed validation marked the incomplete identity proof as valid")
	}

	if err := BackfillActionStorage(t.Context(), f.db); err != nil {
		t.Fatal(err)
	}
	if err := ValidateActionStorageConstraints(t.Context(), f.db); err != nil {
		t.Fatalf("validated the backfilled database: %v", err)
	}
	if f.count(t, `SELECT count(*) FROM pg_constraint WHERE conname IN (
		'action_request_typed_identity_present', 'action_event_actor_proto_present',
		'action_decision_policy_evidence_proto_present') AND convalidated`) != 3 {
		t.Fatal("cutover did not leave all three PostgreSQL proofs validated")
	}
	if f.count(t, `SELECT count(*) FROM action_request WHERE id = $1 AND action_group = 'legacy-group'
		AND action_name = 'legacy-action' AND action = '{"group":"legacy-group","name":"legacy-action"}'::jsonb`, id) != 1 {
		t.Fatal("backfill changed the historical request's identity or JSON content")
	}

	request := f.submit(t, caller, "typed-only-after-validation")
	if f.count(t, `SELECT count(*) FROM action_request WHERE id = $1 AND action IS NULL
		AND action_group = 'test-group' AND action_name = 'test-action'`, request.ID) != 1 {
		t.Fatal("current writer did not store only the typed Action identity")
	}
}
