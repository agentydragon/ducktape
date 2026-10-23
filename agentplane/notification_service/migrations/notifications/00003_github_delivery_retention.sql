-- +goose Up
-- Historical receipts have no ingestion timestamp. Start their retention clock at migration
-- rather than guessing from upstream event timestamps, which may describe older activity.
ALTER TABLE github_delivery ADD COLUMN received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
CREATE INDEX ix_github_delivery_received_at ON github_delivery (received_at, position);

-- +goose Down
DROP INDEX ix_github_delivery_received_at;
ALTER TABLE github_delivery DROP COLUMN received_at;
