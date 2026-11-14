-- +goose Up
-- Sessions written before the execution Identity and caller attribution cutovers cannot be
-- executed or attributed under the current contract. Remove their command receipts with them.
WITH stale_sessions AS (
    SELECT session_id
    FROM session
    WHERE execution_identity_name IS NULL
       OR (creator_account_namespace IS NULL AND creator_identity_name IS NULL)
)
DELETE FROM dispatch AS d
USING stale_sessions AS s
WHERE d.session_id = s.session_id;

DELETE FROM session
WHERE execution_identity_name IS NULL
   OR (creator_account_namespace IS NULL AND creator_identity_name IS NULL);

-- Old internal writers could append commands without a retained actor attribution.
DELETE FROM dispatch
WHERE actor_account_namespace IS NULL
  AND actor_identity_name IS NULL
  AND delegated_identity_name IS NULL;
