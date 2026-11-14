-- +goose Up
ALTER TABLE session_history ADD COLUMN writer_service_uri VARCHAR;
ALTER TABLE session_history DROP CONSTRAINT history_writer_one_kind;
ALTER TABLE session_history ADD CONSTRAINT history_writer_one_kind
    CHECK (num_nonnulls(writer_uid, writer_identity_uid, writer_service_uri) <= 1);
ALTER TABLE session_history ADD CONSTRAINT history_writer_service_uri_valid
    CHECK (writer_service_uri IS NULL OR writer_service_uri LIKE 'spiffe://agentplane/%');

-- +goose Down
-- Refuse a rollback that would erase the provenance of service-owned history.
-- +goose StatementBegin
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM session_history WHERE writer_service_uri IS NOT NULL) THEN
        RAISE EXCEPTION 'service-written history requires ownership cutover before rollback';
    END IF;
END $$;
-- +goose StatementEnd
ALTER TABLE session_history DROP CONSTRAINT history_writer_service_uri_valid;
ALTER TABLE session_history DROP CONSTRAINT history_writer_one_kind;
ALTER TABLE session_history ADD CONSTRAINT history_writer_one_kind
    CHECK (writer_uid IS NULL OR writer_identity_uid IS NULL);
ALTER TABLE session_history DROP COLUMN writer_service_uri;
