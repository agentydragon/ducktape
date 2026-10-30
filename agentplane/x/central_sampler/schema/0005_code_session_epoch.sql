-- +goose Up
ALTER TABLE sampler_conversation ADD COLUMN code_session_epoch INTEGER NOT NULL DEFAULT 0;

-- +goose Down
-- Durable sampler state has no safe downgrade. Restore a database backup to revert a migration.
