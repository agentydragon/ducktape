-- +goose Up
-- Fresh databases create the current schema; completed migration 14 is adopted without rewriting data.
-- +goose StatementBegin
DO $sampler_baseline$
DECLARE
    latest_version BIGINT;
BEGIN
    SELECT COALESCE(max(version_id) FILTER (WHERE is_applied),0) INTO latest_version
    FROM (SELECT DISTINCT ON (version_id) version_id,is_applied
          FROM central_sampler_goose_version ORDER BY version_id,id DESC) AS latest;
    IF latest_version=14 AND NOT EXISTS (SELECT 1 FROM central_sampler_goose_version WHERE version_id>14) THEN
        IF (SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema()
            AND table_type='BASE TABLE' AND table_name IN ('sampler_conversation','sampler_intent','sampler_process','sampler_cell','sampler_interrupt','sampler_session','sampler_command','sampler_event','sampler_export_checkpoint','sampler_notification','sampler_content','sampler_context','sampler_model_request')) <> 13
           OR EXISTS (
            SELECT 1 FROM (VALUES ('sampler_conversation','transcript_context_id','text','YES'),
            ('sampler_conversation','active_context_id','text','YES'),
            ('sampler_conversation','transcript_control','jsonb','YES'),
            ('sampler_content','payload','bytea','NO'),
            ('sampler_context','item_count','bigint','NO'),
            ('sampler_model_request','profile_id','text','NO'),
            ('sampler_model_request','source_context_id','text','YES'),
            ('sampler_intent','result','jsonb','YES'),
            ('sampler_event','entry','bytea','NO')) AS expected(table_name,column_name,data_type,is_nullable)
            WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns actual
                WHERE actual.table_schema=current_schema() AND actual.table_name=expected.table_name
                  AND actual.column_name=expected.column_name AND actual.data_type=expected.data_type
                  AND actual.is_nullable=expected.is_nullable))
           OR (to_regclass('sampler_conversation_item') IS NOT NULL
            OR EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()
                AND table_name='sampler_conversation' AND column_name='context')
            OR to_regclass('sampler_event_sequence_idx') IS NOT NULL
            OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='sampler_event'::regclass
                AND contype='p' AND pg_get_constraintdef(oid)='PRIMARY KEY (session_id, sequence)')) THEN
            RAISE EXCEPTION 'sampler baseline predecessor has an unrecognized schema; repair migration 14 before adoption';
        END IF;
        RETURN;
    END IF;
    IF latest_version<>0 OR EXISTS (SELECT 1 FROM central_sampler_goose_version WHERE version_id>0) THEN
        RAISE EXCEPTION 'sampler baseline requires a fresh database or completed audit rewrite migration 14; run the previous image with writers stopped first';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relnamespace=current_schema()::regnamespace
        AND relkind IN ('r','p','v','m','S','f')
        AND relname NOT IN ('central_sampler_goose_version','central_sampler_goose_version_id_seq')) THEN
        RAISE EXCEPTION 'sampler database has unrecognized objects without migration history';
    END IF;
    CREATE TABLE sampler_conversation (
        id UUID PRIMARY KEY,
        generation INTEGER NOT NULL DEFAULT 0,
        owner UUID,
        lease_until TIMESTAMPTZ,
        pending JSONB,
        stopped BOOLEAN NOT NULL DEFAULT FALSE,
        executor_session VARCHAR,
        instructions TEXT,
        context_tokens INTEGER,
        executor_shell VARCHAR,
        environment_context JSONB,
        code_session_epoch INTEGER NOT NULL DEFAULT 0,
        code_host_instance UUID,
        api TEXT NOT NULL CONSTRAINT sampler_conversation_api_check
            CHECK (api IN ('responses', 'anthropic_messages'))
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
    CREATE UNIQUE INDEX sampler_process_model_session_idx ON sampler_process(conversation_id, model_session_id)
        WHERE model_session_id IS NOT NULL AND status <> 'exited';
    CREATE TABLE sampler_content (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload BYTEA NOT NULL
    );
    CREATE TABLE sampler_context (
        id TEXT PRIMARY KEY,
        api TEXT NOT NULL CHECK (api IN ('responses', 'anthropic_messages')),
        parent_id TEXT REFERENCES sampler_context(id),
        content_id TEXT NOT NULL REFERENCES sampler_content(id),
        item_count BIGINT NOT NULL CHECK (item_count > 0)
    );
    ALTER TABLE sampler_conversation
        ADD COLUMN transcript_context_id TEXT REFERENCES sampler_context(id),
        ADD COLUMN active_context_id TEXT REFERENCES sampler_context(id),
        ADD COLUMN transcript_control JSONB,
        ADD COLUMN active_control JSONB;
    CREATE TABLE sampler_model_request (
        id UUID PRIMARY KEY REFERENCES sampler_intent(id),
        conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
        position BIGINT GENERATED BY DEFAULT AS IDENTITY,
        context_id TEXT REFERENCES sampler_context(id),
        profile_id TEXT NOT NULL REFERENCES sampler_content(id),
        source_context_id TEXT REFERENCES sampler_context(id),
        projection_key TEXT NOT NULL DEFAULT '',
        prefix_ids TEXT[] NOT NULL DEFAULT '{}',
        suffix_ids TEXT[] NOT NULL DEFAULT '{}'
    );
    CREATE INDEX sampler_model_request_latest_idx ON sampler_model_request(conversation_id,position DESC);
END
$sampler_baseline$;
-- +goose StatementEnd

-- +goose Down
-- +goose StatementBegin
DO $$ BEGIN RAISE EXCEPTION 'sampler baseline cannot be downgraded'; END $$;
-- +goose StatementEnd
