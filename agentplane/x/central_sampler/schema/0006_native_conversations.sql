-- +goose Up
-- API classification and archive conversion require inspecting JSON records in Go. Keep the new
-- column nullable until that single transactional backfill succeeds; Migrate then installs its
-- NOT NULL and CHECK constraints.
ALTER TABLE sampler_conversation ADD COLUMN api TEXT;
ALTER TABLE sampler_conversation RENAME COLUMN context_items TO context;
ALTER TABLE sampler_conversation_item RENAME COLUMN item TO record;

-- +goose Down
-- Durable sampler state has no safe downgrade. Restore a database backup to revert a migration.
