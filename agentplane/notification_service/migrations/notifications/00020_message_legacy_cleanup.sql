-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- The JSON/protobuf Message cleanup runs only after canonical TEXT readers and writers are deployed. NOT VALID
-- avoids a serving-init table scan while the constraints immediately reject new legacy Message writes.
ALTER TABLE entry
    ADD CONSTRAINT entry_message_legacy_content_absent CHECK (
        identity_provider IS DISTINCT FROM 'message' OR (payload IS NULL AND typed_payload IS NULL)
    ) NOT VALID,
    ADD CONSTRAINT entry_typed_payload_source_canonical CHECK (
        typed_payload IS NULL OR
        (identity_provider IS NOT NULL AND identity_provider IN ('actions', 'github_mergeability'))
    ) NOT VALID;

-- This checkpoint is the cleanup command's first database read. A later contract drops it before removing the
-- legacy columns, fencing stale cleanup images without running a scan in serving readiness.
CREATE TABLE notification_message_legacy_cleanup (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'cleaning'
        CHECK (phase IN ('cleaning', 'scanned', 'cleaned')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);
INSERT INTO notification_message_legacy_cleanup (singleton) VALUES (true);

-- +goose Down
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Cleanup removes legacy user-authored Message representations. TEXT remains canonical and is not reconstructed
-- into JSON/protobuf by Down; roll application code back only to a TEXT-only-compatible release.
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'Message legacy cleanup is forward-only; keep the migration applied and roll back only to a TEXT-only-compatible release';
END $$;
-- +goose StatementEnd
