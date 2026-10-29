-- +goose Up
ALTER TABLE sampler_conversation
    ADD COLUMN executor_shell VARCHAR,
    ADD COLUMN environment_context JSONB;

-- +goose Down
-- Durable sampler state has no safe downgrade. Restore a database backup to revert a migration.
