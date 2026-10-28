-- +goose Up
-- Init records bounded backfill progress here and builds natural-key indexes concurrently.
SET lock_timeout = '1s';
CREATE TABLE notification_event_identity_backfill (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'backfilling'
        CHECK (phase IN ('backfilling', 'backfilled', 'indexed')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);
RESET lock_timeout;

-- +goose Down
SET lock_timeout = '1s';
DROP TABLE notification_event_identity_backfill;
RESET lock_timeout;
