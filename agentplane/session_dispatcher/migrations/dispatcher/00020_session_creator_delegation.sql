-- +goose Up
-- A trusted platform component may create a native Session as an Identity. Keep the effective
-- creator Identity and the delegating actor's ServiceAccount, Pod and Action correlation separate.
ALTER TABLE session
    ADD COLUMN creator_delegator_account_namespace VARCHAR,
    ADD COLUMN creator_delegator_account_name VARCHAR,
    ADD COLUMN creator_delegator_account_uid VARCHAR,
    ADD COLUMN creator_delegator_pod_name VARCHAR,
    ADD COLUMN creator_delegator_pod_uid VARCHAR,
    ADD COLUMN creator_delegated_action_request_id UUID,
    ADD COLUMN creator_delegated_action_execution_id UUID,
    ADD CONSTRAINT ck_session_creator_delegation CHECK (
        num_nulls(creator_delegator_account_namespace, creator_delegator_account_name,
            creator_delegator_account_uid, creator_delegator_pod_name, creator_delegator_pod_uid,
            creator_delegated_action_request_id, creator_delegated_action_execution_id) IN (0, 7)
        AND (creator_delegator_account_namespace IS NULL OR
            (creator_identity_name IS NOT NULL AND creator_identity_pod_name IS NULL))
    );
