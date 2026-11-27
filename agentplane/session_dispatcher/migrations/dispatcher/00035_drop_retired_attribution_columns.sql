-- +goose Up
-- Retire the ServiceAccount, delegated-Identity and wake-account attribution columns. Serving code
-- stopped reading and writing them one release earlier; rows attributed only through them already
-- read as unattributed. Dropping a column drops every constraint naming it, so the Identity and
-- service attribution invariants that remain are restated without the retired columns.
ALTER TABLE dispatch
    DROP COLUMN wake_eligible,
    DROP COLUMN wake_caller_namespace,
    DROP COLUMN wake_caller_name,
    DROP COLUMN actor_account_namespace,
    DROP COLUMN actor_account_name,
    DROP COLUMN actor_account_uid,
    DROP COLUMN actor_pod_name,
    DROP COLUMN actor_pod_uid,
    DROP COLUMN effective_caller_account_namespace,
    DROP COLUMN effective_caller_account_name,
    DROP COLUMN effective_caller_account_uid,
    DROP COLUMN action_request_id,
    DROP COLUMN action_execution_id,
    DROP COLUMN delegated_identity_name,
    DROP COLUMN delegated_identity_uid,
    DROP COLUMN delegator_account_namespace,
    DROP COLUMN delegator_account_name,
    DROP COLUMN delegator_account_uid,
    DROP COLUMN delegator_pod_name,
    DROP COLUMN delegator_pod_uid,
    DROP COLUMN delegated_action_request_id,
    DROP COLUMN delegated_action_execution_id,
    ADD CONSTRAINT ck_dispatch_actor_identity CHECK (
        (actor_identity_name IS NULL) = (actor_identity_uid IS NULL)
        AND (actor_identity_pod_name IS NULL) = (actor_identity_pod_uid IS NULL)
        AND (actor_identity_pod_name IS NULL OR actor_identity_name IS NOT NULL)),
    ADD CONSTRAINT ck_dispatch_service_attribution CHECK (service_attribution IS NULL OR
        (jsonb_typeof(service_attribution) = 'object' AND actor_identity_name IS NULL));
ALTER TABLE session
    DROP COLUMN wake_eligible,
    DROP COLUMN wake_caller_namespace,
    DROP COLUMN wake_caller_name,
    DROP COLUMN creator_account_namespace,
    DROP COLUMN creator_account_name,
    DROP COLUMN creator_account_uid,
    DROP COLUMN effective_caller_account_namespace,
    DROP COLUMN effective_caller_account_name,
    DROP COLUMN effective_caller_account_uid,
    DROP COLUMN creator_pod_name,
    DROP COLUMN creator_pod_uid,
    DROP COLUMN action_request_id,
    DROP COLUMN action_execution_id,
    DROP COLUMN creator_delegator_account_namespace,
    DROP COLUMN creator_delegator_account_name,
    DROP COLUMN creator_delegator_account_uid,
    DROP COLUMN creator_delegator_pod_name,
    DROP COLUMN creator_delegator_pod_uid,
    DROP COLUMN creator_delegated_action_request_id,
    DROP COLUMN creator_delegated_action_execution_id,
    ADD CONSTRAINT ck_session_creator_identity CHECK (
        (creator_identity_name IS NULL) = (creator_identity_uid IS NULL)
        AND (creator_identity_pod_name IS NULL) = (creator_identity_pod_uid IS NULL)
        AND (creator_identity_pod_name IS NULL OR creator_identity_name IS NOT NULL)),
    ADD CONSTRAINT ck_session_service_attribution CHECK (service_attribution IS NULL OR
        (jsonb_typeof(service_attribution) = 'object' AND creator_identity_name IS NULL));
