-- +goose Up
ALTER TABLE dispatch ADD COLUMN IF NOT EXISTS traceparent VARCHAR NOT NULL DEFAULT '';
ALTER TABLE session_opening ADD COLUMN IF NOT EXISTS traceparent VARCHAR NOT NULL DEFAULT '';

-- +goose Down
ALTER TABLE session_opening DROP COLUMN IF EXISTS traceparent;
ALTER TABLE dispatch DROP COLUMN IF EXISTS traceparent;
