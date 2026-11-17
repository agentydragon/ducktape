-- +goose Up
-- Session ID is already unique per inbox. Expand readers to use it independently of the legacy
-- destination key, and permit future Session-keyed rows to omit Sandbox-only metadata. Old writers
-- continue populating these columns until the writer cutover.
ALTER TABLE inbox
    ALTER COLUMN owner_namespace DROP NOT NULL,
    ALTER COLUMN owner_name DROP NOT NULL,
    ALTER COLUMN destination_namespace DROP NOT NULL,
    ALTER COLUMN destination_name DROP NOT NULL,
    ALTER COLUMN destination_uid DROP NOT NULL;

ALTER TABLE inbox
    DROP CONSTRAINT inbox_destination_fields_valid,
    ADD CONSTRAINT inbox_destination_fields_valid CHECK (
        (destination_namespace IS NULL AND destination_name IS NULL AND destination_uid IS NULL)
        OR
        (destination_namespace IS NOT NULL
            AND char_length(destination_namespace) BETWEEN 1 AND 63
            AND destination_name IS NOT NULL
            AND char_length(destination_name) BETWEEN 1 AND 63
            AND destination_uid IS NOT NULL
            AND char_length(destination_uid) BETWEEN 1 AND 128)
    );
