-- +goose Up
-- The command-side backfill owns progress; Goose only creates this small checkpoint during serving rollout.
SET LOCAL lock_timeout = '1s';
CREATE TABLE notification_entry_content_backfill (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'backfilling'
        CHECK (phase IN ('backfilling', 'scanned', 'backfilled')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);

-- +goose Down
SET LOCAL lock_timeout = '1s';
DROP TABLE notification_entry_content_backfill;
