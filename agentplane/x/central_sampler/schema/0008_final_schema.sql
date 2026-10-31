-- +goose Up
-- A fresh database creates the final native schema. A completed version-7 database is adopted
-- without rewriting retained conversations, commands, evidence, or Goose history.
-- +goose StatementBegin
DO $sampler_final_schema$
DECLARE
    applied_versions INTEGER;
    latest_version BIGINT;
BEGIN
    IF to_regclass(format('%I.%I', current_schema(), 'central_sampler_goose_version')) IS NULL THEN
        RAISE EXCEPTION 'Sampler Goose history is missing';
    END IF;

    WITH latest AS (
        SELECT DISTINCT ON (version_id) version_id, is_applied
        FROM central_sampler_goose_version
        ORDER BY version_id, id DESC
    )
    SELECT count(*) FILTER (WHERE version_id BETWEEN 1 AND 7 AND is_applied),
           COALESCE(max(version_id) FILTER (WHERE is_applied), 0)
    INTO applied_versions, latest_version
    FROM latest;

    IF latest_version = 7 AND applied_versions = 7 THEN
        IF (SELECT count(*) FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
            AND table_name IN (
                'sampler_conversation', 'sampler_conversation_item', 'sampler_intent',
                'sampler_process', 'sampler_cell', 'sampler_interrupt', 'sampler_session',
                'sampler_command', 'sampler_event', 'sampler_export_checkpoint',
                'sampler_notification')) <> 11
            OR EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND (
                    (table_name = 'sampler_conversation' AND column_name = 'context_items') OR
                    (table_name = 'sampler_conversation_item' AND column_name = 'item'))
            )
            OR EXISTS (
                SELECT 1 FROM (VALUES
                    ('sampler_conversation', 'api', 'text', 'NO'),
                    ('sampler_conversation', 'context', 'jsonb', 'YES'),
                    ('sampler_conversation', 'code_session_epoch', 'integer', 'NO'),
                    ('sampler_conversation', 'executor_shell', 'character varying', 'YES'),
                    ('sampler_conversation', 'environment_context', 'jsonb', 'YES'),
                    ('sampler_conversation_item', 'record', 'jsonb', 'NO'),
                    ('sampler_process', 'model_session_id', 'integer', 'YES'),
                    ('sampler_process', 'omitted_bytes', 'bigint', 'NO'),
                    ('sampler_notification', 'sequence', 'bigint', 'NO')
                ) AS expected(table_name, column_name, data_type, is_nullable)
                WHERE NOT EXISTS (
                    SELECT 1 FROM information_schema.columns actual
                    WHERE actual.table_schema = current_schema()
                    AND actual.table_name = expected.table_name
                    AND actual.column_name = expected.column_name
                    AND actual.data_type = expected.data_type
                    AND actual.is_nullable = expected.is_nullable)
            )
            OR (SELECT count(*) FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relnamespace = current_schema()::regnamespace
                AND c.conname IN ('sampler_conversation_api_check',
                    'sampler_process_status_check', 'sampler_cell_status_check')
                AND c.contype = 'c' AND c.convalidated) <> 3
            OR NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relnamespace = current_schema()::regnamespace
                AND t.relname = 'sampler_conversation'
                AND c.conname = 'sampler_conversation_api_check'
                AND c.convalidated
                AND pg_get_constraintdef(c.oid) LIKE '%responses%'
                AND pg_get_constraintdef(c.oid) LIKE '%anthropic_messages%')
            OR (SELECT count(*) FROM pg_index idx
                JOIN pg_class relation ON relation.oid = idx.indexrelid
                WHERE relation.relnamespace = current_schema()::regnamespace
                AND relation.relname IN (
                    'sampler_intent_unresolved_idx', 'sampler_interrupt_pending_idx',
                    'sampler_command_queue_idx', 'sampler_event_sequence_idx',
                    'sampler_process_model_session_idx')
                AND idx.indisvalid AND idx.indisready) <> 5
        THEN
            RAISE EXCEPTION 'Sampler version 7 native schema is incomplete; run the previous image or repair before migration 8';
        END IF;
        RETURN;
    END IF;

    IF latest_version <> 0 OR applied_versions <> 0 THEN
        RAISE EXCEPTION 'Sampler storage requires completed version 7 before migration 8; run the previous image';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class relation
        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = current_schema()
        AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        AND relation.relname NOT IN (
            'central_sampler_goose_version', 'central_sampler_goose_version_id_seq')
    ) THEN
        RAISE EXCEPTION 'Sampler database contains objects without completed version 7 history';
    END IF;

    CREATE TABLE sampler_conversation (
        id UUID PRIMARY KEY,
        generation INTEGER NOT NULL DEFAULT 0,
        owner UUID,
        lease_until TIMESTAMPTZ,
        pending JSONB,
        stopped BOOLEAN NOT NULL DEFAULT FALSE,
        executor_session VARCHAR,
        context JSONB,
        instructions TEXT,
        context_tokens INTEGER,
        executor_shell VARCHAR,
        environment_context JSONB,
        code_session_epoch INTEGER NOT NULL DEFAULT 0,
        api TEXT NOT NULL CONSTRAINT sampler_conversation_api_check
            CHECK (api IN ('responses', 'anthropic_messages'))
    );

    CREATE TABLE sampler_conversation_item (
        conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
        ordinal BIGINT NOT NULL CHECK (ordinal > 0),
        record JSONB NOT NULL,
        PRIMARY KEY (conversation_id, ordinal)
    );

    CREATE TABLE sampler_intent (
        id UUID PRIMARY KEY,
        conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
        generation INTEGER NOT NULL,
        request JSONB NOT NULL,
        result JSONB
    );

    CREATE TABLE sampler_process (
        id VARCHAR PRIMARY KEY,
        conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
        generation INTEGER NOT NULL,
        after_seq INTEGER,
        status VARCHAR NOT NULL CONSTRAINT sampler_process_status_check
            CHECK (status IN ('starting', 'running', 'exited', 'exited_unread', 'abandoned', 'unknown')),
        tty BOOLEAN NOT NULL,
        exit_code INTEGER,
        buffered_output BYTEA NOT NULL DEFAULT ''::bytea,
        model_session_id INTEGER,
        omitted_bytes BIGINT NOT NULL DEFAULT 0
    );

    CREATE TABLE sampler_cell (
        id VARCHAR NOT NULL,
        conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
        generation INTEGER NOT NULL,
        status VARCHAR NOT NULL CONSTRAINT sampler_cell_status_check
            CHECK (status IN ('live', 'completed', 'terminated', 'abandoned')),
        PRIMARY KEY (id, conversation_id, generation)
    );

    CREATE TABLE sampler_interrupt (
        id UUID PRIMARY KEY,
        conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
        generation INTEGER NOT NULL,
        target_turn VARCHAR,
        result JSONB
    );

    CREATE TABLE sampler_session (
        id VARCHAR PRIMARY KEY,
        conversation_id UUID NOT NULL UNIQUE REFERENCES sampler_conversation(id),
        account_uid VARCHAR NOT NULL,
        configuration BYTEA NOT NULL,
        model VARCHAR NOT NULL,
        effort VARCHAR NOT NULL,
        source_id VARCHAR NOT NULL,
        cursor INTEGER NOT NULL DEFAULT 0,
        active_turn VARCHAR,
        active_command VARCHAR,
        workspace_prepared BOOLEAN NOT NULL DEFAULT FALSE,
        setup_state INTEGER NOT NULL DEFAULT 1,
        setup_started BOOLEAN NOT NULL DEFAULT FALSE,
        blocked BOOLEAN NOT NULL DEFAULT FALSE,
        blocked_reason VARCHAR
    );

    CREATE TABLE sampler_command (
        session_id VARCHAR NOT NULL REFERENCES sampler_session(id),
        id VARCHAR NOT NULL,
        payload BYTEA NOT NULL,
        admission INTEGER NOT NULL,
        outcome INTEGER NOT NULL,
        failure_reason VARCHAR,
        PRIMARY KEY (session_id, id)
    );

    CREATE TABLE sampler_event (
        session_id VARCHAR NOT NULL REFERENCES sampler_session(id),
        sequence INTEGER NOT NULL,
        entry BYTEA NOT NULL,
        PRIMARY KEY (session_id, sequence)
    );

    CREATE TABLE sampler_export_checkpoint (
        session_id VARCHAR NOT NULL REFERENCES sampler_session(id),
        destination VARCHAR NOT NULL,
        cursor INTEGER NOT NULL,
        PRIMARY KEY (session_id, destination)
    );

    CREATE TABLE sampler_notification (
        conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
        sequence BIGINT GENERATED ALWAYS AS IDENTITY,
        call_id VARCHAR NOT NULL,
        cell_id VARCHAR NOT NULL,
        text TEXT NOT NULL,
        PRIMARY KEY (conversation_id, sequence)
    );

    CREATE INDEX sampler_intent_unresolved_idx ON sampler_intent(conversation_id) WHERE result IS NULL;
    CREATE INDEX sampler_interrupt_pending_idx ON sampler_interrupt(conversation_id, generation) WHERE result IS NULL;
    CREATE INDEX sampler_command_queue_idx ON sampler_command(session_id, admission) WHERE outcome = 1;
    CREATE INDEX sampler_event_sequence_idx ON sampler_event(session_id, sequence);
    CREATE UNIQUE INDEX sampler_process_model_session_idx ON sampler_process(conversation_id, model_session_id)
        WHERE model_session_id IS NOT NULL AND status <> 'exited';
END
$sampler_final_schema$;
-- +goose StatementEnd

-- +goose Down
-- Durable sampler state has no safe downgrade. Restore a database backup to revert a migration.
