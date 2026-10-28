SET LOCAL lock_timeout = '5s';

-- The stopped old writers have finished and native backfills are complete. Keep the old JSON
-- columns until final validation, while installing proofs for the final writer's typed fields.
ALTER TABLE subscription ADD CONSTRAINT subscription_creation_typed_required
    CHECK (creation_proto IS NOT NULL AND source_provider IS NOT NULL) NOT VALID;

ALTER TABLE inbox ADD CONSTRAINT inbox_destination_fields_valid CHECK (
    destination_namespace IS NOT NULL AND char_length(destination_namespace) BETWEEN 1 AND 63 AND
    destination_name IS NOT NULL AND char_length(destination_name) BETWEEN 1 AND 63 AND
    destination_uid IS NOT NULL AND char_length(destination_uid) BETWEEN 1 AND 128
) NOT VALID;

ALTER TABLE entry ADD CONSTRAINT entry_identity_provider_required
    CHECK (identity_provider IS NOT NULL) NOT VALID;

ALTER TABLE notification_event_identity_backfill
    DROP CONSTRAINT notification_event_identity_backfill_phase_check,
    ADD CONSTRAINT notification_event_identity_backfill_phase_check
        CHECK (phase IN ('backfilling', 'backfilled', 'indexed', 'writer_only'));

-- Once cleanup clears legacy payloads, the old content backfill must never run again.
DROP TABLE notification_entry_content_backfill;

-- NOT VALID admits retained legacy JSON until cleanup removes it; every subsequent update
-- must satisfy the check, so cleanup cannot introduce a new owned JSON value.
ALTER TABLE entry ADD CONSTRAINT entry_owned_legacy_content_absent CHECK (
    identity_provider IS NULL OR identity_provider NOT IN ('actions', 'github_mergeability', 'message') OR payload IS NULL
) NOT VALID;

CREATE TABLE notification_entry_legacy_cleanup (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'cleaning' CHECK (phase IN ('cleaning', 'scanned', 'cleaned')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);
INSERT INTO notification_entry_legacy_cleanup (singleton) VALUES (true);
