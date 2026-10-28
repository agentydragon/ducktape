-- +goose Up
SET LOCAL lock_timeout = '5s';

-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM inbox LIMIT 1) AND NOT EXISTS (
        SELECT 1 FROM notification_storage_backfill_progress
        WHERE singleton AND inbox_destination_complete
    ) THEN
        RAISE EXCEPTION 'inbox destination backfill must complete before the typed destination contract';
    END IF;
END $$;
-- +goose StatementEnd

ALTER TABLE inbox
    ALTER COLUMN destination_ref DROP NOT NULL,
    ADD CONSTRAINT inbox_destination_fields_valid CHECK (
        destination_namespace IS NOT NULL AND char_length(destination_namespace) BETWEEN 1 AND 63 AND
        destination_name IS NOT NULL AND char_length(destination_name) BETWEEN 1 AND 63 AND
        destination_uid IS NOT NULL AND char_length(destination_uid) BETWEEN 1 AND 128
    ) NOT VALID;

-- +goose Down
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- A final-schema writer may leave JSON NULL, so refuse Down until retained copies have been restored.
-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM inbox WHERE destination_ref IS NULL LIMIT 1) THEN
        RAISE EXCEPTION 'destination_ref JSON has NULL rows; retain the typed contract';
    END IF;
END $$;
-- +goose StatementEnd

ALTER TABLE inbox
    DROP CONSTRAINT inbox_destination_fields_valid,
    ALTER COLUMN destination_ref SET NOT NULL;
