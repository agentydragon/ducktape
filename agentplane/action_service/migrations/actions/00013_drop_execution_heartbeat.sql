-- +goose Up
ALTER TABLE action_execution DROP COLUMN heartbeat_at;

-- +goose Down
ALTER TABLE action_execution ADD COLUMN heartbeat_at TIMESTAMPTZ;
