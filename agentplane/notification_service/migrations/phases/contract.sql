SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- Only durable catalog and checkpoint proofs are inspected here. All retained-row validation
-- ran before this transaction, while the old JSON columns were still intact.
DO $$
BEGIN
    IF (SELECT phase FROM notification_entry_legacy_cleanup WHERE singleton) IS DISTINCT FROM 'cleaned'
        OR (SELECT phase FROM notification_event_identity_backfill WHERE singleton) IS DISTINCT FROM 'writer_only'
        OR (SELECT inbox_destination_complete AND subscription_creation_complete
                FROM notification_storage_backfill_progress WHERE singleton) IS DISTINCT FROM TRUE
    THEN
        RAISE EXCEPTION 'Notification cutover checkpoints are incomplete';
    END IF;

    IF (SELECT count(*) FROM pg_constraint
        WHERE conrelid = 'inbox'::regclass AND conname = 'inbox_destination_fields_valid' AND convalidated) <> 1
        OR (SELECT count(*) FROM pg_constraint WHERE conrelid = 'subscription'::regclass
            AND conname IN ('subscription_creation_storage_pair', 'subscription_source_provider',
                'subscription_typed_source_state', 'subscription_creation_typed_required') AND convalidated) <> 4
        OR (SELECT count(*) FROM pg_constraint WHERE conrelid = 'entry'::regclass
            AND conname IN ('entry_identity_shape', 'entry_identity_provider_required',
                'entry_action_content_source', 'entry_mergeability_content_source', 'entry_message_content_source',
                'entry_action_content_shape', 'entry_mergeability_content_shape', 'entry_message_content_shape',
                'entry_message_body_bytes', 'entry_message_from_session_length', 'entry_action_actor_length',
                'entry_owned_legacy_content_absent') AND convalidated) <> 12
    THEN
        RAISE EXCEPTION 'Notification cutover constraints are not all validated';
    END IF;

    IF (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE i.indrelid = 'entry'::regclass AND n.nspname = current_schema()
            AND c.relname IN ('entry_identity_actions_key', 'entry_identity_github_key',
                'entry_identity_github_observation_key', 'entry_identity_mergeability_sequence_key',
                'entry_identity_mergeability_snapshot_key', 'entry_identity_message_key')
            AND i.indisvalid AND i.indisready AND i.indisunique) <> 6
    THEN
        RAISE EXCEPTION 'Notification cutover natural-key indexes are incomplete';
    END IF;
END $$;

-- Validated CHECK constraints let PostgreSQL set NOT NULL without a retained-row scan.
ALTER TABLE inbox
    ALTER COLUMN destination_namespace SET NOT NULL,
    ALTER COLUMN destination_name SET NOT NULL,
    ALTER COLUMN destination_uid SET NOT NULL,
    DROP COLUMN destination_ref;

ALTER TABLE subscription
    ALTER COLUMN creation_proto SET NOT NULL,
    ALTER COLUMN source_provider SET NOT NULL,
    DROP CONSTRAINT subscription_creation_source,
    DROP CONSTRAINT subscription_source_state,
    DROP CONSTRAINT subscription_creation_storage_pair,
    DROP CONSTRAINT subscription_creation_typed_required,
    DROP COLUMN creation;

ALTER TABLE entry
    ALTER COLUMN identity_provider SET NOT NULL,
    DROP CONSTRAINT entry_identity_provider_required,
    DROP CONSTRAINT entry_message_body_ceiling,
    DROP CONSTRAINT entry_inbox_id_event_key,
    DROP COLUMN event;

DROP TABLE notification_storage_backfill_progress;
DROP TABLE notification_event_identity_backfill;
DROP TABLE notification_entry_legacy_cleanup;
