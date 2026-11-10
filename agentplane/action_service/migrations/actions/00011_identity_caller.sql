-- +goose Up
-- A request is a ServiceAccount's (caller_namespace, caller_name, caller_uid) or an Identity incarnation's
-- (caller_identity_name, caller_identity_uid). A release before this one scans caller_namespace and caller_name as
-- set, so it fails a read that meets an Identity's request: nothing writes one until every replica runs this release.
ALTER TABLE action_request
    ALTER COLUMN caller_namespace DROP NOT NULL,
    ALTER COLUMN caller_name DROP NOT NULL,
    ADD COLUMN caller_identity_name TEXT,
    ADD COLUMN caller_identity_uid TEXT,
    ADD CONSTRAINT action_request_one_caller CHECK (
        (caller_namespace IS NOT NULL AND caller_name IS NOT NULL
            AND caller_identity_name IS NULL AND caller_identity_uid IS NULL)
        OR (caller_namespace IS NULL AND caller_name IS NULL AND caller_uid IS NULL
            AND caller_identity_name <> '' AND caller_identity_uid <> '')),
    ADD CONSTRAINT action_request_identity_idempotency_key
        UNIQUE (caller_identity_name, caller_identity_uid, idempotency_key);
