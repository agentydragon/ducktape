-- +goose Up
-- Sessions written before the execution Identity and caller attribution cutovers cannot be
-- executed or attributed under the current contract. Remove their command receipts with them.
WITH stale_sessions AS (
    SELECT session_id
    FROM session
    WHERE execution_identity_name IS NULL
       OR num_nonnulls(
            execution_account_namespace, execution_account_name, execution_account_uid,
            creator_account_namespace, creator_account_name, creator_account_uid,
            creator_pod_name, creator_pod_uid,
            creator_identity_name, creator_identity_uid, creator_identity_pod_name, creator_identity_pod_uid,
            effective_caller_account_namespace, effective_caller_account_name, effective_caller_account_uid,
            action_request_id, action_execution_id, wake_caller_namespace, wake_caller_name
       ) = 0
)
DELETE FROM dispatch AS d
USING stale_sessions AS s
WHERE d.session_id = s.session_id;

DELETE FROM session
WHERE execution_identity_name IS NULL
   OR num_nonnulls(
        execution_account_namespace, execution_account_name, execution_account_uid,
        creator_account_namespace, creator_account_name, creator_account_uid, creator_pod_name, creator_pod_uid,
        creator_identity_name, creator_identity_uid, creator_identity_pod_name, creator_identity_pod_uid,
        effective_caller_account_namespace, effective_caller_account_name, effective_caller_account_uid,
        action_request_id, action_execution_id, wake_caller_namespace, wake_caller_name
   ) = 0;

-- Keep any standalone dispatch receipt with actor, caller, Pod, delegation or Action evidence.
DELETE FROM dispatch
WHERE num_nonnulls(
    actor_account_namespace, actor_account_name, actor_account_uid, actor_pod_name, actor_pod_uid,
    actor_identity_name, actor_identity_uid, actor_identity_pod_name, actor_identity_pod_uid,
    delegated_identity_name, delegated_identity_uid,
    delegator_account_namespace, delegator_account_name, delegator_account_uid, delegator_pod_name, delegator_pod_uid,
    delegated_action_request_id, delegated_action_execution_id,
    effective_caller_account_namespace, effective_caller_account_name, effective_caller_account_uid,
    action_request_id, action_execution_id, wake_caller_namespace, wake_caller_name
) = 0;
