-- +goose Up
-- A Session survives loss of its required Environment. This is independent of
-- materialization: the owner may have accepted the Session before the loss.
ALTER TABLE session ADD COLUMN environment_deleted_at TIMESTAMPTZ;

-- +goose Down
ALTER TABLE session DROP COLUMN environment_deleted_at;
