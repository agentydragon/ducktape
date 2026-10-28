-- +goose Up
SET lock_timeout = '1s';
ALTER TABLE entry
    ADD COLUMN typed_payload BYTEA,
    ADD CONSTRAINT entry_typed_payload_source CHECK (
        typed_payload IS NULL OR
        (identity_provider IS NOT NULL AND identity_provider IN ('actions', 'github_mergeability', 'message'))
    ) NOT VALID;
RESET lock_timeout;

-- +goose Down
SET lock_timeout = '1s';
ALTER TABLE entry DROP CONSTRAINT entry_typed_payload_source;
ALTER TABLE entry DROP COLUMN typed_payload;
RESET lock_timeout;
