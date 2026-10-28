-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Init applies this guard after all old writers stop. NOT VALID admits retained JSON until
-- bounded cleanup removes it, while rejecting new owned JSON writes.
ALTER TABLE entry ADD CONSTRAINT entry_owned_legacy_content_absent CHECK (
    identity_provider IS NULL OR identity_provider NOT IN ('actions', 'github_mergeability', 'message') OR payload IS NULL
) NOT VALID;

-- Init cleanup reads this checkpoint before touching entry. Migration 00023 drops it after proof.
CREATE TABLE notification_entry_legacy_cleanup (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'cleaning' CHECK (phase IN ('cleaning', 'scanned', 'cleaned')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);
INSERT INTO notification_entry_legacy_cleanup (singleton) VALUES (true);

-- +goose Down
SET LOCAL lock_timeout = '1s';
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'owned legacy JSON cleanup is forward-only; retain migration 00019';
END $$;
-- +goose StatementEnd
