-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Apply only through the explicit cleanup command after all preceding dual writers drain, never in serving init.
-- New native-only writers must never restore owned JSON. NOT VALID leaves historical dual-write rows for the
-- offline cleanup while immediately rejecting new legacy content.
ALTER TABLE entry ADD CONSTRAINT entry_owned_legacy_content_absent CHECK (
    identity_provider IS NULL OR identity_provider NOT IN ('actions', 'github_mergeability', 'message') OR payload IS NULL
) NOT VALID;

-- The cleanup command reads this checkpoint before touching entry. Migration 00023 drops it to fence stale jobs.
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
    RAISE EXCEPTION 'owned legacy JSON cleanup is forward-only; keep migration 00019 applied and roll back only to a native-content-capable release';
END $$;
-- +goose StatementEnd
