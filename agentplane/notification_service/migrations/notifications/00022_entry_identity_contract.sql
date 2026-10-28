-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Populated databases reach this contract only after init validation has published the durable
-- writer-only phase. Inspect that metadata before probing entry.
-- A fresh empty database can validate the same checks synchronously, then publish the phase itself.
-- +goose StatementBegin
DO $$
DECLARE
    backfill_phase VARCHAR;
    valid_index_count INTEGER;
    proof_count INTEGER;
    validated_proof_count INTEGER;
    entry_has_rows BOOLEAN;
BEGIN
    SELECT phase INTO backfill_phase
    FROM notification_event_identity_backfill WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'event identity contract checkpoint is missing; apply migrations through 00018 first';
    END IF;

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
    IF valid_index_count <> 6 THEN
        RAISE EXCEPTION 'event identity contract requires all six valid natural-key indexes; found %', valid_index_count;
    END IF;

    SELECT count(*), count(*) FILTER (WHERE convalidated)
    INTO proof_count, validated_proof_count
    FROM pg_constraint
    WHERE conrelid = 'entry'::regclass
        AND conname IN (
            'entry_identity_shape',
            'entry_identity_provider_required',
            'entry_message_body_bytes'
        );
    IF proof_count <> 3 THEN
        RAISE EXCEPTION 'event identity contract requires all three validated identity and Message proofs; found % constraints', proof_count;
    END IF;

    IF backfill_phase = 'writer_only' AND validated_proof_count = proof_count THEN
        RETURN;
    END IF;

    IF backfill_phase NOT IN ('indexed', 'writer_only') THEN
        RAISE EXCEPTION 'event identity contract requires indexed or writer_only backfill phase; found %', backfill_phase;
    END IF;

    SELECT EXISTS (SELECT 1 FROM entry LIMIT 1) INTO entry_has_rows;
    IF entry_has_rows THEN
        RAISE EXCEPTION 'event identity contract requires completed backfill and validated proofs before migration 00022';
    END IF;

    ALTER TABLE entry
        VALIDATE CONSTRAINT entry_identity_shape,
        VALIDATE CONSTRAINT entry_identity_provider_required,
        VALIDATE CONSTRAINT entry_message_body_bytes;
    UPDATE notification_event_identity_backfill SET phase = 'writer_only' WHERE singleton;
END $$;
-- +goose StatementEnd

-- The validated provider check lets PostgreSQL set NOT NULL without another retained-row scan. The JSONB event
-- form is no longer read by current code; preserve relational identity keys and the Message body ceiling proof.
ALTER TABLE entry ALTER COLUMN identity_provider SET NOT NULL;
ALTER TABLE entry
    DROP CONSTRAINT entry_identity_provider_required,
    DROP CONSTRAINT entry_message_body_ceiling,
    DROP CONSTRAINT entry_inbox_id_event_key,
    DROP COLUMN event;

DROP TABLE notification_event_identity_backfill;

-- +goose Down
SET LOCAL lock_timeout = '1s';
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'event identity JSON was retired; retain the relational identity contract';
END $$;
-- +goose StatementEnd
