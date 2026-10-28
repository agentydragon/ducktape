-- +goose Up
-- The explicit offline Message text command owns this resumable checkpoint. A later writer release drops it before
-- legacy Message JSON stops being written, so stale copies of the command fail before reading entry rows.
SET LOCAL lock_timeout = '1s';
CREATE TABLE notification_message_text_backfill (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'backfilling'
        CHECK (phase IN ('backfilling', 'scanned', 'backfilled')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);
INSERT INTO notification_message_text_backfill (singleton) VALUES (true);

-- +goose Down
SET LOCAL lock_timeout = '1s';
DROP TABLE notification_message_text_backfill;
