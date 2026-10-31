-- +goose Up
-- A nullable marker keeps old binaries able to serve during a rolling migration.
ALTER TABLE sampler_conversation ADD COLUMN code_host_instance UUID;

-- +goose Down
-- Durable sampler state has no safe downgrade. Restore a database backup to revert a migration.
