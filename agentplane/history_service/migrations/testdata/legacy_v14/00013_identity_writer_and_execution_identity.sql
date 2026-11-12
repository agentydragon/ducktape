-- +goose Up
-- A Session's ingress writer is a ServiceAccount (writer_namespace, writer_name, writer_uid) or an
-- Identity incarnation (writer_identity_name, writer_identity_uid), never both: an older replica that
-- reads an Identity-bound row as unbound fails this constraint instead of binding a ServiceAccount.
-- execution_identity_{name,uid} is the Identity the Dispatcher recorded the Session as executing as,
-- beside the execution ServiceAccount principal.
ALTER TABLE session_history
    ADD COLUMN writer_identity_name VARCHAR,
    ADD COLUMN writer_identity_uid VARCHAR,
    ADD COLUMN execution_identity_name VARCHAR,
    ADD COLUMN execution_identity_uid VARCHAR,
    ADD CONSTRAINT history_writer_identity_complete
        CHECK (num_nulls(writer_identity_name, writer_identity_uid) IN (0, 2)),
    ADD CONSTRAINT history_writer_one_kind CHECK (writer_uid IS NULL OR writer_identity_uid IS NULL),
    ADD CONSTRAINT history_execution_identity_complete
        CHECK (num_nulls(execution_identity_name, execution_identity_uid) IN (0, 2));
CREATE INDEX ix_history_execution_identity_recorded
    ON session_history (execution_identity_name, execution_identity_uid, recorded_position)
    WHERE execution_identity_uid IS NOT NULL;
