-- +goose Up
ALTER TABLE checkpoint ADD COLUMN model TEXT;
ALTER TABLE checkpoint ADD COLUMN reasoning_effort TEXT;

-- +goose Down
ALTER TABLE checkpoint DROP COLUMN reasoning_effort;
ALTER TABLE checkpoint DROP COLUMN model;
