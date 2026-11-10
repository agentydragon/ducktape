-- +goose Up
-- The Identity a Session's Sandbox named at creation, beside its ServiceAccount execution principal.
-- Nullable and additive: a replica that predates it never writes it and reads the row as before.
ALTER TABLE session
    ADD COLUMN execution_identity_name VARCHAR,
    ADD COLUMN execution_identity_uid UUID,
    ADD CONSTRAINT ck_session_execution_identity
        CHECK ((execution_identity_name IS NULL) = (execution_identity_uid IS NULL));
