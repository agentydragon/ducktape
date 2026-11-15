-- +goose Up
-- A command a trusted platform component submitted on behalf of an Identity records that Identity,
-- the component's ServiceAccount and Pod, and the Action it served, in columns of their own. A
-- replica that predates them reads such a row as unattributed, never as a ServiceAccount or a
-- direct Identity attribution. No Session is opened this way, so the session table has none.
ALTER TABLE dispatch
    ADD COLUMN delegated_identity_name VARCHAR,
    ADD COLUMN delegated_identity_uid VARCHAR,
    ADD COLUMN delegator_account_namespace VARCHAR,
    ADD COLUMN delegator_account_name VARCHAR,
    ADD COLUMN delegator_account_uid VARCHAR,
    ADD COLUMN delegator_pod_name VARCHAR,
    ADD COLUMN delegator_pod_uid VARCHAR,
    ADD COLUMN delegated_action_request_id UUID,
    ADD COLUMN delegated_action_execution_id UUID,
    ADD CONSTRAINT ck_dispatch_delegated_identity CHECK (
        num_nulls(delegated_identity_name, delegated_identity_uid, delegator_account_namespace,
            delegator_account_name, delegator_account_uid, delegator_pod_name, delegator_pod_uid,
            delegated_action_request_id, delegated_action_execution_id) IN (0, 9)
        AND (delegated_identity_name IS NULL OR num_nonnulls(actor_account_namespace, actor_pod_name,
            effective_caller_account_namespace, action_request_id, actor_identity_name) = 0));
