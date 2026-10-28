-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- The old command's expiry rule treats payload NULL as authoritative. With native content, NULL legacy JSON
-- is also normal, so retire that checkpoint before the final writer starts.
-- +goose StatementBegin
DO $$
DECLARE
    entry_has_rows BOOLEAN;
    content_backfill_complete BOOLEAN;
BEGIN
    SELECT EXISTS (SELECT 1 FROM entry LIMIT 1) INTO entry_has_rows;
    IF entry_has_rows THEN
        SELECT EXISTS (
            SELECT 1 FROM notification_entry_content_backfill WHERE phase = 'backfilled'
        ) INTO content_backfill_complete;
        IF NOT content_backfill_complete THEN
            RAISE EXCEPTION 'entry content backfill is incomplete before the native content contract';
        END IF;
    END IF;
END $$;
-- +goose StatementEnd

-- All entries must have a relational provider before the identity contract makes the column required.
-- The init migrator validates this constraint after the write fence and before the final contract.
ALTER TABLE entry ADD CONSTRAINT entry_identity_provider_required
    CHECK (identity_provider IS NOT NULL) NOT VALID;

DROP TABLE notification_entry_content_backfill;

-- +goose Down
SET LOCAL lock_timeout = '1s';

-- Restoring the checkpoint after native-only writes would make the old content command unsafe.
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'native-only entry writes retired the content backfill checkpoint; retain this migration';
END $$;
-- +goose StatementEnd
