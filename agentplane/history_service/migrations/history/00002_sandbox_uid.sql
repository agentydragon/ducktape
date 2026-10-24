-- +goose Up
ALTER TABLE session_history ADD COLUMN sandbox_uid VARCHAR;

-- +goose Down
ALTER TABLE session_history DROP COLUMN sandbox_uid;
