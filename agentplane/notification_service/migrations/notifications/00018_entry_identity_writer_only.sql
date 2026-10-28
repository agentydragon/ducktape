-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Do not race the resumable identity backfill or concurrent index builds. This transaction-scoped lock is
-- released automatically if this migration is interrupted.
-- +goose StatementBegin
DO $$
BEGIN
    IF NOT pg_try_advisory_xact_lock(hashtextextended('notification_event_identity_backfill', 0)) THEN
        RAISE EXCEPTION 'event identity backfill is active; retry migration 00018 after it finishes';
    END IF;
END $$;
-- +goose StatementEnd

-- Populated databases use only checkpoint and catalog state before the short constraint DDL below. The table
-- lock is needed only for a fresh, empty database that must create the natural-key indexes synchronously.
-- +goose StatementBegin
DO $$
DECLARE
    entry_has_rows BOOLEAN;
    backfill_phase VARCHAR;
    expected_index RECORD;
    index_is_ready BOOLEAN;
    valid_index_count INTEGER;
    indexed_fast_path BOOLEAN := FALSE;
BEGIN
    SELECT phase INTO backfill_phase
    FROM notification_event_identity_backfill WHERE singleton;
    IF FOUND AND backfill_phase IN ('indexed', 'writer_only') THEN
        SELECT count(*) INTO valid_index_count
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE i.indrelid = 'entry'::regclass
            AND n.nspname = current_schema()
            AND c.relname IN (
                'entry_identity_actions_key',
                'entry_identity_github_key',
                'entry_identity_github_observation_key',
                'entry_identity_mergeability_sequence_key',
                'entry_identity_mergeability_snapshot_key',
                'entry_identity_message_key'
            )
            AND i.indisvalid AND i.indisready AND i.indisunique;
        indexed_fast_path := valid_index_count = 6;
    END IF;

    IF NOT indexed_fast_path THEN
        SELECT EXISTS (SELECT 1 FROM entry LIMIT 1) INTO entry_has_rows;
        IF entry_has_rows THEN
            RAISE EXCEPTION 'event identity indexes are not ready before the identity contract (phase %, valid indexes %)',
                COALESCE(backfill_phase, '<missing>'), COALESCE(valid_index_count, 0);
        END IF;

        -- Close the race with an unexpected concurrent writer: prove emptiness again while holding the table lock.
        LOCK TABLE entry IN ACCESS EXCLUSIVE MODE;
        SELECT EXISTS (SELECT 1 FROM entry LIMIT 1) INTO entry_has_rows;
        IF entry_has_rows THEN
            RAISE EXCEPTION 'entries appeared while preparing fresh database identity indexes; retry init';
        END IF;

        FOR expected_index IN
            SELECT * FROM (VALUES
                ('entry_identity_actions_key',
                    'CREATE UNIQUE INDEX entry_identity_actions_key ON entry (inbox_id, actions_request_id, actions_sequence) WHERE identity_provider = ''actions'''),
                ('entry_identity_github_key',
                    'CREATE UNIQUE INDEX entry_identity_github_key ON entry (inbox_id, github_app_id, github_delivery_id, github_repository_id, github_event, github_action) NULLS NOT DISTINCT WHERE identity_provider = ''github'''),
                ('entry_identity_github_observation_key',
                    'CREATE UNIQUE INDEX entry_identity_github_observation_key ON entry (inbox_id, github_app_id, github_repository_id, github_event, github_observation_sequence) WHERE identity_provider = ''github_observation'''),
                ('entry_identity_mergeability_sequence_key',
                    'CREATE UNIQUE INDEX entry_identity_mergeability_sequence_key ON entry (inbox_id, github_app_id, github_repository_id, github_pull_request_number, github_mergeability_sequence) WHERE identity_provider = ''github_mergeability'' AND github_mergeability_sequence IS NOT NULL'),
                ('entry_identity_mergeability_snapshot_key',
                    'CREATE UNIQUE INDEX entry_identity_mergeability_snapshot_key ON entry (inbox_id, github_app_id, github_repository_id, github_pull_request_number, github_snapshot_version) WHERE identity_provider = ''github_mergeability'' AND github_snapshot_version IS NOT NULL'),
                ('entry_identity_message_key',
                    'CREATE UNIQUE INDEX entry_identity_message_key ON entry (inbox_id, message_id, message_sender_namespace, message_sender_name) WHERE identity_provider = ''message''')
            ) AS indexes(index_name, index_ddl)
        LOOP
            SELECT i.indisvalid AND i.indisready AND i.indisunique INTO index_is_ready
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE i.indrelid = 'entry'::regclass
                AND n.nspname = current_schema()
                AND c.relname = expected_index.index_name;
            IF FOUND THEN
                IF NOT index_is_ready THEN
                    RAISE EXCEPTION 'event identity index % exists but is not valid and unique; repair it before migration 00018', expected_index.index_name;
                END IF;
            ELSE
                EXECUTE expected_index.index_ddl;
            END IF;
        END LOOP;

        INSERT INTO notification_event_identity_backfill (singleton, last_inbox_id, last_cursor, phase)
        VALUES (true, NULL, NULL, 'indexed')
        ON CONFLICT (singleton) DO UPDATE SET last_inbox_id = NULL, last_cursor = NULL, phase = 'indexed';
    END IF;
END $$;
-- +goose StatementEnd

-- The final writer uses relational identity. Init validates the retained provider proof before contract.
ALTER TABLE entry ALTER COLUMN event DROP NOT NULL;

ALTER TABLE notification_event_identity_backfill
    DROP CONSTRAINT notification_event_identity_backfill_phase_check,
    ADD CONSTRAINT notification_event_identity_backfill_phase_check
        CHECK (phase IN ('backfilling', 'backfilled', 'indexed', 'writer_only'));

-- +goose Down
SET LOCAL lock_timeout = '1s';
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'event JSON writes have stopped; retain migration 00018';
END $$;
-- +goose StatementEnd
