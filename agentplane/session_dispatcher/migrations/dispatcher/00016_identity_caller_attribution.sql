-- +goose Up
-- Rows written for a caller whose workload SVID named an Identity record that Identity and its Pod
-- in their own columns. A replica that predates them reads such a row as unattributed, never as a
-- partial ServiceAccount attribution.
ALTER TABLE session
    ADD COLUMN creator_identity_name VARCHAR,
    ADD COLUMN creator_identity_uid VARCHAR,
    ADD COLUMN creator_identity_pod_name VARCHAR,
    ADD COLUMN creator_identity_pod_uid VARCHAR,
    ADD CONSTRAINT ck_session_creator_identity CHECK (
        (creator_identity_name IS NULL) = (creator_identity_uid IS NULL)
        AND (creator_identity_pod_name IS NULL) = (creator_identity_pod_uid IS NULL)
        AND (creator_identity_pod_name IS NULL OR creator_identity_name IS NOT NULL)
        AND (creator_identity_name IS NULL OR creator_account_namespace IS NULL));
ALTER TABLE dispatch
    ADD COLUMN actor_identity_name VARCHAR,
    ADD COLUMN actor_identity_uid VARCHAR,
    ADD COLUMN actor_identity_pod_name VARCHAR,
    ADD COLUMN actor_identity_pod_uid VARCHAR,
    ADD CONSTRAINT ck_dispatch_actor_identity CHECK (
        (actor_identity_name IS NULL) = (actor_identity_uid IS NULL)
        AND (actor_identity_pod_name IS NULL) = (actor_identity_pod_uid IS NULL)
        AND (actor_identity_pod_name IS NULL OR actor_identity_name IS NOT NULL)
        AND (actor_identity_name IS NULL OR actor_account_namespace IS NULL));
