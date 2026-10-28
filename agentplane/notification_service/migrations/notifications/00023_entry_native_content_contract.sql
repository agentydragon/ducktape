-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Cleanup and this contract use the same advisory lock. After this migration drops its checkpoint,
-- an accidental later cleanup fails before it touches entries.
-- +goose StatementBegin
DO $$
DECLARE
    entry_has_rows BOOLEAN;
    proof_count INTEGER;
    validated_proof_count INTEGER;
    cleanup_phase VARCHAR;
    proof_name TEXT;
    proof_validated BOOLEAN;
BEGIN
    IF NOT pg_try_advisory_xact_lock(hashtextextended('notification_entry_legacy_cleanup', 0)) THEN
        RAISE EXCEPTION 'entry legacy content cleanup is active; retry migration 00023 after it finishes';
    END IF;

    IF to_regclass('notification_entry_legacy_cleanup') IS NULL THEN
        RAISE EXCEPTION 'entry legacy cleanup checkpoint is missing; apply migrations through 00019 first';
    END IF;

    SELECT count(*), count(*) FILTER (WHERE convalidated)
    INTO proof_count, validated_proof_count
    FROM pg_constraint
    WHERE conrelid = 'entry'::regclass
        AND contype = 'c'
        AND conname = ANY (ARRAY[
            'entry_action_content_source',
            'entry_mergeability_content_source',
            'entry_message_content_source',
            'entry_action_content_shape',
            'entry_mergeability_content_shape',
            'entry_message_content_shape',
            'entry_message_body_bytes',
            'entry_message_from_session_length',
            'entry_action_actor_length',
            'entry_owned_legacy_content_absent'
        ]);
    IF proof_count <> 10 THEN
        RAISE EXCEPTION 'entry native-content contract requires all ten native-content constraints; found %', proof_count;
    END IF;

    SELECT phase INTO cleanup_phase
    FROM notification_entry_legacy_cleanup WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'entry legacy cleanup checkpoint has no singleton row';
    END IF;

    -- The completed proofs are sufficient for populated databases; avoid probing or scanning retained entries
    -- after validation.
    IF cleanup_phase = 'cleaned' AND validated_proof_count = proof_count THEN
        RETURN;
    END IF;

    SELECT EXISTS (SELECT 1 FROM entry LIMIT 1) INTO entry_has_rows;
    IF entry_has_rows THEN
        IF validated_proof_count <> proof_count THEN
            RAISE EXCEPTION 'entry native-content contract requires all ten native-content constraints to be validated on a populated database; found % of %',
                validated_proof_count, proof_count;
        END IF;
        RAISE EXCEPTION 'entry legacy content cleanup phase is %, want cleaned before migration 00023', cleanup_phase;
    END IF;

    -- Close the race with an unexpected writer. Only the empty-database path takes this table lock or validates
    -- constraints inline; populated databases use the durable catalog and checkpoint proofs above.
    LOCK TABLE entry IN ACCESS EXCLUSIVE MODE;
    SELECT EXISTS (SELECT 1 FROM entry LIMIT 1) INTO entry_has_rows;
    IF entry_has_rows THEN
        IF validated_proof_count <> proof_count THEN
            RAISE EXCEPTION 'entry native-content contract requires all ten native-content constraints to be validated on a populated database; found % of %',
                validated_proof_count, proof_count;
        END IF;
        RAISE EXCEPTION 'entry legacy content cleanup phase is %, want cleaned before migration 00023', cleanup_phase;
    END IF;

    -- A fresh empty database has no historical rows for cleanup to visit. Validate the permanent
    -- checks here while the empty entry table remains locked, then discard the checkpoint below.
    FOR proof_name IN
        SELECT unnest(ARRAY[
            'entry_action_content_source',
            'entry_mergeability_content_source',
            'entry_message_content_source',
            'entry_action_content_shape',
            'entry_mergeability_content_shape',
            'entry_message_content_shape',
            'entry_message_body_bytes',
            'entry_message_from_session_length',
            'entry_action_actor_length',
            'entry_owned_legacy_content_absent'
        ])
    LOOP
        SELECT convalidated INTO proof_validated
        FROM pg_constraint
        WHERE conrelid = 'entry'::regclass AND contype = 'c' AND conname = proof_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'entry native-content constraint % disappeared before validation', proof_name;
        END IF;
        IF NOT proof_validated THEN
            EXECUTE format('ALTER TABLE entry VALIDATE CONSTRAINT %I', proof_name);
        END IF;
    END LOOP;
END $$;
-- +goose StatementEnd

-- Drop cleanup's first-read fence. Retained native constraints and GitHub JSON payloads remain.
DROP TABLE notification_entry_legacy_cleanup;

-- +goose Down
SET LOCAL lock_timeout = '1s';
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'native content contract is forward-only; keep migration 00023 applied and never recreate the legacy cleanup checkpoint';
END $$;
-- +goose StatementEnd
