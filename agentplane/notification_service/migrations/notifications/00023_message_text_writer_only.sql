-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Once this checkpoint is retired, an older Message text backfill binary must fail before scanning or changing
-- entries. The checkpoint is the command's first database object, so DROP TABLE also waits for any active batch.
-- +goose StatementBegin
DO $$
DECLARE
    entry_has_rows BOOLEAN;
    message_text_backfill_complete BOOLEAN;
BEGIN
    SELECT EXISTS (SELECT 1 FROM entry LIMIT 1) INTO entry_has_rows;
    IF entry_has_rows THEN
        SELECT EXISTS (
            SELECT 1 FROM notification_message_text_backfill WHERE phase = 'backfilled'
        ) INTO message_text_backfill_complete;
        IF NOT message_text_backfill_complete THEN
            RAISE EXCEPTION 'Message text backfill is incomplete; finish backfill-message-text on the dual-write image before deploying the text-only writer';
        END IF;
    END IF;
END $$;
-- +goose StatementEnd

DROP TABLE notification_message_text_backfill;

-- +goose Down
SET LOCAL lock_timeout = '1s';

-- Restoring this checkpoint would make the old command appear runnable after text-only writes begin.
-- Keep the migration applied and roll application code back only to the canonical-text reader release.
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'text-only Message writes retired the backfill checkpoint; keep this migration applied and roll back to a canonical-text reader release';
END $$;
-- +goose StatementEnd
