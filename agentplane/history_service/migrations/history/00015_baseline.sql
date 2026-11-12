-- +goose Up
-- Create the final schema on a fresh database or adopt a completed version-14 database.
-- Earlier deployments must run the previous image through version 14 and finish that rollout first.
-- Keep the prior Goose rows as audit history even though migrations 11-14 are no longer shipped.
-- +goose StatementBegin
DO $history_baseline$
DECLARE
    current_versions INTEGER;
    applied_current_versions INTEGER;
    historical_versions INTEGER;
    applied_historical_versions INTEGER;
    latest_version BIGINT;
BEGIN
    IF to_regclass(format('%I.%I', current_schema(), 'history_service_goose_version')) IS NULL THEN
        RAISE EXCEPTION 'History Goose table is missing';
    END IF;
    WITH latest AS (
        SELECT DISTINCT ON (version_id) version_id, is_applied
        FROM history_service_goose_version
        ORDER BY version_id, id DESC
    )
    SELECT count(*) FILTER (WHERE version_id BETWEEN 11 AND 14),
           count(*) FILTER (WHERE is_applied AND version_id BETWEEN 11 AND 14),
           count(*) FILTER (WHERE version_id BETWEEN 1 AND 10),
           count(*) FILTER (WHERE is_applied AND version_id BETWEEN 1 AND 10),
           COALESCE(max(version_id) FILTER (WHERE is_applied), 0)
    INTO current_versions, applied_current_versions, historical_versions,
         applied_historical_versions, latest_version
    FROM latest;
    IF latest_version = 0 AND current_versions = 0 AND historical_versions = 0
       AND NOT EXISTS (SELECT 1 FROM history_service_goose_version WHERE version_id > 0) THEN
        IF EXISTS (
            SELECT 1 FROM pg_class c WHERE c.relnamespace=current_schema()::regnamespace
              AND c.relkind IN ('r','p','v','m','S','f')
              AND c.relname NOT IN ('history_service_goose_version','history_service_goose_version_id_seq')
        ) THEN
            RAISE EXCEPTION 'History database contains objects without migration history; refusing baseline adoption';
        END IF;
        RETURN;
    END IF;
    IF latest_version <> 14 OR current_versions <> 4 OR applied_current_versions <> 4
       OR NOT (historical_versions = 0 AND applied_historical_versions = 0)
          AND NOT (historical_versions = 10 AND applied_historical_versions = 10)
       OR EXISTS (SELECT 1 FROM history_service_goose_version WHERE version_id > 14) THEN
        RAISE EXCEPTION 'History baseline 15 requires a fresh database or completed migration version 14 from a recognized version-10 or fresh-schema history; run the previous image through version 14 and let its rollout finish before starting this image';
    END IF;
    IF (SELECT count(*) FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
          AND table_name IN ('session_history','session_event','session_event_settlement',
            'session_event_settled_range','session_event_removed_range','session_projection_lease',
            'thread_checkpoint','thread_history_summary','thread_entity','thread_evidence',
            'thread_native_link','thread_payload_manifest','thread_payload_chunk','session_fold_pending')) <> 14 THEN
        RAISE EXCEPTION 'History migration version 14 is incomplete; run the previous image through version 14 before starting this image';
    END IF;
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('session_history','recorded_position','bigint','NO'),
            ('session_history','sandbox_uid','character varying','YES'),
            ('session_history','execution_principal_uid','character varying','YES'),
            ('session_history','writer_identity_name','character varying','YES'),
            ('session_history','writer_identity_uid','character varying','YES'),
            ('session_history','execution_identity_name','character varying','YES'),
            ('session_history','execution_identity_uid','character varying','YES'),
            ('session_event','kind','integer','NO'),
            ('thread_entity','state','jsonb','NO'),
            ('thread_entity','entity_index','bigint','NO'),
            ('session_fold_pending','version','bigint','NO')
        ) AS expected(table_name,column_name,data_type,is_nullable)
        WHERE NOT EXISTS (
            SELECT 1 FROM information_schema.columns actual
            WHERE actual.table_schema=current_schema()
              AND actual.table_name=expected.table_name
              AND actual.column_name=expected.column_name
              AND actual.data_type=expected.data_type
              AND actual.is_nullable=expected.is_nullable
        )
    ) OR EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND
          ((table_name='session_history' AND column_name='opened_position') OR
           (table_name='thread_entity' AND column_name IN
             ('turn_id','text_ref','arguments_ref','output_ref','input_ref')))
    ) THEN
        RAISE EXCEPTION 'History migration version 14 has an unrecognized table shape; run the previous image through version 14 before starting this image';
    END IF;
    IF (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        WHERE c.relnamespace=current_schema()::regnamespace AND c.relname IN
          ('ux_history_recorded_position','ix_history_execution_principal_recorded',
           'ix_session_event_kind_cursor','ix_thread_entity_scope_entity_index',
           'ix_history_execution_identity_recorded')
          AND i.indisvalid AND i.indisready) <> 5
       OR (SELECT count(*) FROM pg_constraint
           WHERE conrelid='session_history'::regclass AND contype='c' AND convalidated
             AND conname IN ('history_writer_identity_complete','history_writer_one_kind',
                             'history_execution_identity_complete')) <> 3
       OR (SELECT count(*) FROM pg_trigger
           WHERE tgrelid='session_history'::regclass AND NOT tgisinternal
             AND tgname IN ('session_history_fold_pending_insert','session_history_fold_pending_update')) <> 2
       OR EXISTS (SELECT 1 FROM pg_trigger
                  WHERE tgrelid='thread_entity'::regclass AND NOT tgisinternal
                    AND tgname='thread_entity_command_operation_enum')
       OR EXISTS (SELECT 1 FROM pg_proc
                  WHERE pronamespace=current_schema()::regnamespace
                    AND proname='thread_command_operation_enum')
       OR to_regclass('session_fold_pending_version') IS NULL
       OR pg_get_serial_sequence('session_history','recorded_position') IS NULL
       OR EXISTS (SELECT 1 FROM pg_class WHERE oid IN (
            'thread_checkpoint'::regclass, 'thread_entity'::regclass,
            'thread_payload_manifest'::regclass, 'thread_payload_chunk'::regclass
          ) AND relreplident <> 'f')
       OR (SELECT count(*) FROM pg_publication_tables
           WHERE pubname='electric_publication_agentplane_history'
             AND schemaname=current_schema() AND tablename IN
             ('thread_checkpoint','thread_entity','thread_payload_manifest','thread_payload_chunk')) <> 4
       OR (SELECT count(*) FROM (VALUES
            ('thread_checkpoint'),('thread_entity'),('thread_payload_manifest'),('thread_payload_chunk')
          ) AS replicated(table_name)
          WHERE NOT has_table_privilege('electric', format('%I.%I',current_schema(),replicated.table_name),'SELECT')) > 0 THEN
        RAISE EXCEPTION 'History migration version 14 schema is incomplete or still has the temporary command bridge; run the previous image through version 14 before starting this image';
    END IF;
END
$history_baseline$;
-- +goose StatementEnd
CREATE TABLE IF NOT EXISTS session_history (
    id UUID PRIMARY KEY,
    source_id VARCHAR,
    last_cursor BIGINT NOT NULL,
    feed_state BYTEA,
    writer_namespace VARCHAR,
    writer_name VARCHAR,
    writer_uid VARCHAR,
    writer_identity_name VARCHAR,
    writer_identity_uid VARCHAR,
    recorded_position BIGINT GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    sandbox_uid VARCHAR,
    execution_principal_namespace VARCHAR,
    execution_principal_name VARCHAR,
    execution_principal_uid VARCHAR,
    execution_identity_name VARCHAR,
    execution_identity_uid VARCHAR,
    sandbox_namespace VARCHAR,
    sandbox_name VARCHAR,
    CONSTRAINT history_last_cursor_nonnegative CHECK (last_cursor >= 0),
    CONSTRAINT history_writer_complete CHECK (num_nulls(writer_namespace, writer_name, writer_uid) IN (0, 3)),
    CONSTRAINT history_writer_identity_complete CHECK (num_nulls(writer_identity_name, writer_identity_uid) IN (0, 2)),
    CONSTRAINT history_writer_one_kind CHECK (writer_uid IS NULL OR writer_identity_uid IS NULL),
    CONSTRAINT history_sandbox_complete CHECK (num_nulls(sandbox_namespace, sandbox_name) IN (0, 2)),
    CONSTRAINT history_execution_principal_complete CHECK (num_nulls(execution_principal_namespace, execution_principal_name, execution_principal_uid) IN (0, 3)),
    CONSTRAINT history_execution_identity_complete CHECK (num_nulls(execution_identity_name, execution_identity_uid) IN (0, 2))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_history_recorded_position ON session_history (recorded_position);
CREATE INDEX IF NOT EXISTS ix_history_execution_principal_recorded
    ON session_history (execution_principal_namespace, execution_principal_name, execution_principal_uid, recorded_position);
CREATE INDEX IF NOT EXISTS ix_history_execution_identity_recorded
    ON session_history (execution_identity_name, execution_identity_uid, recorded_position)
    WHERE execution_identity_uid IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_event (
    session_id UUID NOT NULL REFERENCES session_history (id),
    cursor BIGINT NOT NULL,
    payload BYTEA NOT NULL,
    kind INTEGER NOT NULL,
    PRIMARY KEY (session_id, cursor)
);

CREATE INDEX IF NOT EXISTS ix_session_event_kind_cursor ON session_event (session_id, kind, cursor);

CREATE TABLE IF NOT EXISTS session_event_settlement (
    session_id UUID NOT NULL REFERENCES session_history (id),
    completion_cursor BIGINT NOT NULL,
    payload BYTEA NOT NULL,
    PRIMARY KEY (session_id, completion_cursor)
);

CREATE TABLE IF NOT EXISTS session_event_settled_range (
    session_id UUID NOT NULL,
    first_cursor BIGINT NOT NULL,
    last_cursor BIGINT NOT NULL,
    completion_cursor BIGINT NOT NULL,
    PRIMARY KEY (session_id, first_cursor),
    CONSTRAINT settled_range_ordered CHECK (first_cursor <= last_cursor),
    CONSTRAINT settled_range_settlement FOREIGN KEY (session_id, completion_cursor)
        REFERENCES session_event_settlement (session_id, completion_cursor)
);

CREATE TABLE IF NOT EXISTS session_event_removed_range (
    session_id UUID NOT NULL REFERENCES session_history (id),
    first_cursor BIGINT NOT NULL,
    last_cursor BIGINT NOT NULL,
    PRIMARY KEY (session_id, first_cursor),
    CONSTRAINT removed_range_ordered CHECK (first_cursor <= last_cursor)
);

CREATE TABLE IF NOT EXISTS session_projection_lease (
    session_id UUID PRIMARY KEY,
    token UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_checkpoint (
    thread_id UUID PRIMARY KEY,
    source_id TEXT,
    projection_epoch TEXT NOT NULL,
    through_cursor BIGINT NOT NULL,
    CONSTRAINT thread_checkpoint_source_prefix CHECK (source_id IS NOT NULL OR through_cursor = 0)
);

CREATE TABLE IF NOT EXISTS thread_history_summary (
    thread_id UUID PRIMARY KEY,
    last_event_at TIMESTAMPTZ,
    last_turn_status BIGINT,
    attached JSONB,
    "end" JSONB,
    last_model_activity_at TIMESTAMPTZ,
    first_event_at TIMESTAMPTZ,
    idle_since TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_thread_history_summary_first_event
    ON thread_history_summary (first_event_at, thread_id);

CREATE TABLE IF NOT EXISTS thread_entity (
    thread_id UUID NOT NULL,
    projection_epoch TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    cursor BIGINT NOT NULL,
    revision_cursor BIGINT NOT NULL,
    pending BOOLEAN NOT NULL,
    state JSONB NOT NULL,
    entity_index BIGINT NOT NULL,
    PRIMARY KEY (thread_id, projection_epoch, entity_kind, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_thread_entity_scope_cursor
    ON thread_entity (thread_id, projection_epoch, cursor, entity_kind, entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_thread_entity_scope_entity_index
    ON thread_entity (thread_id, projection_epoch, entity_index);
CREATE INDEX IF NOT EXISTS ix_thread_entity_scope_pending_cursor
    ON thread_entity (thread_id, projection_epoch, cursor, entity_kind, entity_id) WHERE pending;
CREATE INDEX IF NOT EXISTS ix_thread_entity_scope_revision
    ON thread_entity (thread_id, projection_epoch, revision_cursor);
CREATE INDEX IF NOT EXISTS ix_thread_entity_scope_segment_cursor
    ON thread_entity (thread_id, projection_epoch, cursor)
    WHERE entity_kind IN ('item', 'confirmed_input', 'lifecycle');

CREATE TABLE IF NOT EXISTS thread_evidence (
    thread_id UUID NOT NULL,
    projection_epoch TEXT NOT NULL,
    entity_cursor BIGINT NOT NULL,
    observation_cursor BIGINT NOT NULL,
    PRIMARY KEY (thread_id, projection_epoch, entity_cursor, observation_cursor)
);

CREATE TABLE IF NOT EXISTS thread_native_link (
    thread_id UUID NOT NULL,
    projection_epoch TEXT NOT NULL,
    entity_cursor BIGINT NOT NULL,
    observation_cursor BIGINT NOT NULL,
    source_sequence BIGINT NOT NULL,
    PRIMARY KEY (thread_id, projection_epoch, entity_cursor, observation_cursor, source_sequence)
);

CREATE TABLE IF NOT EXISTS thread_payload_manifest (
    thread_id UUID NOT NULL,
    projection_epoch TEXT NOT NULL,
    owner_cursor BIGINT NOT NULL,
    owner_id TEXT NOT NULL,
    field TEXT NOT NULL,
    generation BIGINT NOT NULL,
    revision_cursor BIGINT NOT NULL,
    chunk_count BIGINT NOT NULL,
    content_bytes BIGINT NOT NULL,
    PRIMARY KEY (thread_id, projection_epoch, owner_cursor, owner_id, field, generation, revision_cursor)
);

CREATE TABLE IF NOT EXISTS thread_payload_chunk (
    thread_id UUID NOT NULL,
    projection_epoch TEXT NOT NULL,
    owner_cursor BIGINT NOT NULL,
    owner_id TEXT NOT NULL,
    field TEXT NOT NULL,
    generation BIGINT NOT NULL,
    chunk_index BIGINT NOT NULL,
    text JSON NOT NULL,
    PRIMARY KEY (thread_id, projection_epoch, owner_cursor, owner_id, field, generation, chunk_index)
);

CREATE SEQUENCE IF NOT EXISTS session_fold_pending_version;
CREATE TABLE IF NOT EXISTS session_fold_pending (
    session_id UUID PRIMARY KEY REFERENCES session_history (id) ON DELETE CASCADE,
    version BIGINT NOT NULL
);
-- +goose StatementBegin
CREATE OR REPLACE FUNCTION mark_session_fold_pending() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO session_fold_pending (session_id, version)
        VALUES (NEW.id, nextval('session_fold_pending_version'))
        ON CONFLICT (session_id) DO UPDATE SET version = EXCLUDED.version;
    RETURN NULL;
END $$;
-- +goose StatementEnd
CREATE OR REPLACE TRIGGER session_history_fold_pending_insert AFTER INSERT ON session_history
    FOR EACH ROW EXECUTE FUNCTION mark_session_fold_pending();
CREATE OR REPLACE TRIGGER session_history_fold_pending_update AFTER UPDATE OF last_cursor, feed_state ON session_history
    FOR EACH ROW WHEN (OLD.last_cursor IS DISTINCT FROM NEW.last_cursor OR OLD.feed_state IS DISTINCT FROM NEW.feed_state)
    EXECUTE FUNCTION mark_session_fold_pending();

-- Electric replicates these four tables. The `electric` role is the cluster's replication login.
ALTER TABLE thread_checkpoint REPLICA IDENTITY FULL;
ALTER TABLE thread_entity REPLICA IDENTITY FULL;
ALTER TABLE thread_payload_manifest REPLICA IDENTITY FULL;
ALTER TABLE thread_payload_chunk REPLICA IDENTITY FULL;
GRANT USAGE ON SCHEMA public TO electric;
GRANT SELECT ON thread_checkpoint, thread_entity, thread_payload_manifest, thread_payload_chunk TO electric;
-- +goose StatementBegin
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'electric_publication_agentplane_history') THEN
        CREATE PUBLICATION electric_publication_agentplane_history
            FOR TABLE thread_checkpoint, thread_entity, thread_payload_manifest, thread_payload_chunk;
    END IF;
END $$;
-- +goose StatementEnd
