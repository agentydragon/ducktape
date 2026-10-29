-- +goose Up
-- Model-facing unified exec session numbers, unique among a conversation's unreleased processes.
-- Setup processes are not model-visible and carry none.
ALTER TABLE sampler_process ADD COLUMN model_session_id INTEGER;
-- Bytes dropped between the retained head and tail of unread output.
ALTER TABLE sampler_process ADD COLUMN omitted_bytes BIGINT NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX sampler_process_model_session_idx ON sampler_process(conversation_id, model_session_id)
    WHERE model_session_id IS NOT NULL AND status <> 'exited';

-- +goose Down
-- Durable sampler state has no safe downgrade. Restore a database backup to revert a migration.
