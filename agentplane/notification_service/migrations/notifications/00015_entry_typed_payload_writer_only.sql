-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- The old command's expiry rule treats payload NULL as authoritative. Once a typed-only writer is live, NULL
-- legacy JSON is also normal, so the command must become impossible before that writer is deployed. Its checkpoint
-- table is the first object the command touches; dropping it under the catalog lock also waits out any active batch.
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
            RAISE EXCEPTION 'entry content backfill is incomplete; finish backfill-entry-content on the dual-write image before deploying the typed-only writer';
        END IF;
    END IF;
END $$;
-- +goose StatementEnd

-- All entries must have a relational provider before the identity contract can make the column required. Leave
-- validation to the explicit offline validator so retained rows are scanned outside the serving Pod init path.
ALTER TABLE entry ADD CONSTRAINT entry_identity_provider_required
    CHECK (identity_provider IS NOT NULL) NOT VALID;

DROP TABLE notification_entry_content_backfill;

-- +goose Down
SET LOCAL lock_timeout = '1s';

-- Restoring the checkpoint would make the old content command appear runnable after typed-only writes have begun.
-- Keep this migration applied and roll application code back only to a typed-capable dual-write release.
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'typed-only entry writes retired the content backfill checkpoint; keep this migration applied and roll back to a typed-capable dual-write release';
END $$;
-- +goose StatementEnd
