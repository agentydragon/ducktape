-- +goose Up
-- Typed-only writers leave the old JSON identity absent. The legacy JSON checks stay NOT VALID until the
-- explicit offline validation runs after every writer has moved to this format.
SET LOCAL lock_timeout = '1s';

ALTER TABLE action_request ALTER COLUMN action DROP NOT NULL;

ALTER TABLE action_request ADD CONSTRAINT action_request_typed_identity_present
    CHECK (action_group IS NOT NULL AND action_name IS NOT NULL) NOT VALID;
ALTER TABLE action_event ADD CONSTRAINT action_event_actor_proto_present
    CHECK (actor IS NULL OR actor = 'null'::jsonb OR actor_proto IS NOT NULL) NOT VALID;
ALTER TABLE action_decision ADD CONSTRAINT action_decision_policy_evidence_proto_present
    CHECK (policy_evidence IS NULL OR policy_evidence = 'null'::jsonb OR policy_evidence_proto IS NOT NULL) NOT VALID;

-- The timestamp is a distinct rollout attestation, not a substitute for the validated constraints. The explicit
-- command sets it only when an operator confirms that every live writer uses the typed-only image and validation
-- succeeds. A later contract migration requires both this attestation and PostgreSQL's convalidated proof.
CREATE TABLE action_storage_cutover (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    typed_only_writers_at TIMESTAMPTZ
);
INSERT INTO action_storage_cutover (singleton) VALUES (TRUE);

-- +goose Down
DO $$ BEGIN
    RAISE EXCEPTION 'Action typed-only storage is irreversible; roll back the service image and retain expanded columns';
END $$;
