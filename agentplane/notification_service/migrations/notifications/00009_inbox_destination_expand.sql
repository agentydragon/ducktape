-- +goose Up
SET LOCAL lock_timeout = '5s';
-- Nullable fields let the application dual-write relational destination keys while old rows remain readable
-- from destination_ref. A separate, explicit backfill command runs after every writer has upgraded.
ALTER TABLE inbox
    ADD COLUMN destination_namespace VARCHAR,
    ADD COLUMN destination_name VARCHAR,
    ADD COLUMN destination_uid VARCHAR;

CREATE TABLE notification_storage_backfill_progress (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    inbox_destination_last_id UUID,
    inbox_destination_complete BOOLEAN NOT NULL DEFAULT false
);
INSERT INTO notification_storage_backfill_progress (singleton) VALUES (true);

-- +goose Down
SET LOCAL lock_timeout = '5s';
DROP TABLE notification_storage_backfill_progress;
ALTER TABLE inbox DROP COLUMN destination_uid, DROP COLUMN destination_name, DROP COLUMN destination_namespace;
