-- +goose Up
-- The populated-database proof must come from the separately run validation command on the typed-writer image.
-- Only a genuinely empty database validates inline so fresh Goose installs traverse the complete migration history.
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';

-- +goose StatementBegin
DO $$
DECLARE
    validated_proofs BIGINT;
BEGIN
    SELECT count(*) INTO validated_proofs
    FROM (VALUES
        ('action_request'::regclass, 'action_request_typed_identity_present'),
        ('action_event'::regclass, 'action_event_actor_proto_present'),
        ('action_decision'::regclass, 'action_decision_policy_evidence_proto_present')
    ) AS expected(table_oid, constraint_name)
    JOIN pg_constraint AS proof
        ON proof.conrelid = expected.table_oid
        AND proof.conname = expected.constraint_name
        AND proof.contype = 'c'
        AND proof.convalidated;

    IF validated_proofs <> 3 THEN
        IF NOT EXISTS (SELECT 1 FROM action_request LIMIT 1)
            AND NOT EXISTS (SELECT 1 FROM action_event LIMIT 1)
            AND NOT EXISTS (SELECT 1 FROM action_decision LIMIT 1) THEN
            EXECUTE 'ALTER TABLE action_request VALIDATE CONSTRAINT action_request_typed_identity_present';
            EXECUTE 'ALTER TABLE action_event VALIDATE CONSTRAINT action_event_actor_proto_present';
            EXECUTE 'ALTER TABLE action_decision VALIDATE CONSTRAINT action_decision_policy_evidence_proto_present';
        ELSE
            RAISE EXCEPTION 'Action storage contract requires typed-only writers and offline validation; run validate-storage --confirm-all-writers-typed-only on the typed-writer image before deploying this contract';
        END IF;
    END IF;
END
$$;
-- +goose StatementEnd

-- The validated check proves both columns non-NULL, allowing PostgreSQL to enforce NOT NULL without another table scan.
ALTER TABLE action_request ALTER COLUMN action_group SET NOT NULL;
ALTER TABLE action_request ALTER COLUMN action_name SET NOT NULL;

ALTER TABLE action_request DROP CONSTRAINT action_request_typed_identity_present;
ALTER TABLE action_event DROP CONSTRAINT action_event_actor_proto_present;
ALTER TABLE action_decision DROP CONSTRAINT action_decision_policy_evidence_proto_present;

ALTER TABLE action_request DROP COLUMN action;
ALTER TABLE action_event DROP COLUMN actor;
ALTER TABLE action_decision DROP COLUMN policy_evidence;

-- +goose Down
-- +goose StatementBegin
DO $$ BEGIN
    RAISE EXCEPTION 'Action storage contract is irreversible; rollback only to a typed-only compatible image or restore the database';
END $$;
-- +goose StatementEnd
