-- +goose Up
-- The app schema is currently at Alembic head 0036_auto_thread_titles. On an existing database this
-- migration adopts that exact schema without changing its rows. On a fresh database it creates the
-- same schema and writes the Alembic head marker so the previous Python image can still be restored.
-- +goose StatementBegin
DO $$
DECLARE
    revision_count bigint;
    revision text;
    partial_objects text[];
BEGIN
    IF to_regclass('alembic_version_app') IS NULL THEN
        SELECT array_agg(name ORDER BY name)
          INTO partial_objects
          FROM unnest(ARRAY[
              'thread',
              'operator_browser_session',
              'event',
              'feed_state',
              'event_log',
              'sandbox_ingestion'
          ]) AS candidate(name)
         WHERE to_regclass(name) IS NOT NULL;

        IF partial_objects IS NOT NULL THEN
            RAISE EXCEPTION
                'refusing to initialize app schema without alembic_version_app; found app tables: %',
                partial_objects;
        END IF;
    ELSE
        SELECT count(*), min(version_num)
          INTO revision_count, revision
          FROM alembic_version_app;

        IF revision_count <> 1 OR revision IS DISTINCT FROM '0036_auto_thread_titles' THEN
            RAISE EXCEPTION
                'refusing to adopt app schema at Alembic revision % (expected exactly 0036_auto_thread_titles)',
                revision;
        END IF;

        IF to_regclass('thread') IS NULL OR to_regclass('operator_browser_session') IS NULL THEN
            RAISE EXCEPTION 'refusing to adopt incomplete app schema at Alembic head';
        END IF;

        IF EXISTS (
            SELECT 1
              FROM (VALUES
                  ('thread', 'id', 'uuid', 'NO'),
                  ('thread', 'name', 'text', 'YES'),
                  ('thread', 'archived', 'boolean', 'NO'),
                  ('thread', 'created_at', 'timestamp with time zone', 'NO'),
                  ('thread', 'auto_title_on_first_message', 'boolean', 'NO'),
                  ('thread', 'title_user_managed', 'boolean', 'NO'),
                  ('thread', 'title_status', 'text', 'NO'),
                  ('thread', 'title_scan_cursor', 'bigint', 'NO'),
                  ('thread', 'title_claimed_at', 'timestamp with time zone', 'YES'),
                  ('thread', 'title_revision', 'integer', 'NO'),
                  ('operator_browser_session', 'id', 'text', 'NO'),
                  ('operator_browser_session', 'expires_at', 'timestamp with time zone', 'NO'),
                  ('operator_browser_session', 'payload', 'jsonb', 'NO'),
                  ('operator_browser_session', 'absolute_expires_at', 'timestamp with time zone', 'NO'),
                  ('operator_browser_session', 'operator_issuer', 'text', 'YES'),
                  ('operator_browser_session', 'operator_subject', 'text', 'YES'),
                  ('operator_browser_session', 'operator_username', 'text', 'YES'),
                  ('operator_browser_session', 'access_token', 'text', 'YES'),
                  ('operator_browser_session', 'access_token_expires_at', 'timestamp with time zone', 'YES'),
                  ('operator_browser_session', 'refresh_token', 'text', 'YES')
              ) AS expected(table_name, column_name, data_type, is_nullable)
              LEFT JOIN information_schema.columns AS actual
                ON actual.table_schema = current_schema()
               AND actual.table_name = expected.table_name
               AND actual.column_name = expected.column_name
             WHERE actual.column_name IS NULL
                OR actual.data_type <> expected.data_type
                OR actual.is_nullable <> expected.is_nullable
        ) THEN
            RAISE EXCEPTION 'refusing to adopt app schema with missing or incompatible columns';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint AS c
             WHERE c.conrelid = 'thread'::regclass AND c.contype = 'p'
               AND (
                   SELECT array_agg(a.attname::text ORDER BY key.ordinality)
                     FROM unnest(c.conkey) WITH ORDINALITY AS key(attnum, ordinality)
                     JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = key.attnum
               ) = ARRAY['id']::text[]
        ) OR NOT EXISTS (
            SELECT 1 FROM pg_constraint AS c
             WHERE c.conrelid = 'operator_browser_session'::regclass AND c.contype = 'p'
               AND (
                   SELECT array_agg(a.attname::text ORDER BY key.ordinality)
                     FROM unnest(c.conkey) WITH ORDINALITY AS key(attnum, ordinality)
                     JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = key.attnum
               ) = ARRAY['id']::text[]
        ) OR EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid IN ('thread'::regclass, 'operator_browser_session'::regclass)
               AND contype = 'f'
        ) OR EXISTS (
            SELECT expected.name
              FROM (VALUES
                  ('operator_browser_session_operator_whole', 'operator_issuerisnull=operator_subjectisnullandoperator_issuerisnull=operator_usernameisnull'),
                  ('operator_browser_session_access_token_whole', 'access_tokenisnull=access_token_expires_atisnull'),
                  ('operator_browser_session_tokens_logged_in', 'access_tokenisnulloroperator_issuerisnotnull'),
                  ('operator_browser_session_refresh_token_with_access_token', 'refresh_tokenisnulloraccess_tokenisnotnull')
              ) AS expected(name, expression)
              LEFT JOIN pg_constraint AS actual
                ON actual.conrelid = 'operator_browser_session'::regclass
               AND actual.contype = 'c'
               AND actual.conname = expected.name
             WHERE actual.oid IS NULL
                OR regexp_replace(lower(pg_get_expr(actual.conbin, actual.conrelid)), '[[:space:]()]', '', 'g') <> expected.expression
        ) OR NOT EXISTS (
            SELECT 1
              FROM pg_index AS i
              JOIN pg_class AS idx ON idx.oid = i.indexrelid
              JOIN pg_am AS am ON am.oid = idx.relam
              JOIN pg_attribute AS indexed_column
                ON indexed_column.attrelid = i.indrelid AND indexed_column.attnum = i.indkey[0]
             WHERE i.indrelid = 'operator_browser_session'::regclass
               AND idx.relname = 'ix_operator_browser_session_expires_at'
               AND i.indisvalid AND i.indisready AND NOT i.indisunique
               AND i.indnkeyatts = 1 AND i.indnatts = 1
               AND indexed_column.attname = 'expires_at'
               AND am.amname = 'btree'
        ) THEN
            RAISE EXCEPTION 'refusing to adopt app schema with missing constraints or indexes';
        END IF;
    END IF;
END $$;
-- +goose StatementEnd

CREATE TABLE IF NOT EXISTS thread (
    id UUID PRIMARY KEY,
    name TEXT,
    archived BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL,
    auto_title_on_first_message BOOLEAN NOT NULL DEFAULT false,
    title_user_managed BOOLEAN NOT NULL DEFAULT false,
    title_status TEXT NOT NULL DEFAULT 'waiting',
    title_scan_cursor BIGINT NOT NULL DEFAULT 0,
    title_claimed_at TIMESTAMPTZ,
    title_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS operator_browser_session (
    id TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    operator_issuer TEXT,
    operator_subject TEXT,
    operator_username TEXT,
    access_token TEXT,
    access_token_expires_at TIMESTAMPTZ,
    refresh_token TEXT,
    CONSTRAINT operator_browser_session_operator_whole CHECK (
        (operator_issuer IS NULL) = (operator_subject IS NULL)
        AND (operator_issuer IS NULL) = (operator_username IS NULL)
    ),
    CONSTRAINT operator_browser_session_access_token_whole CHECK (
        (access_token IS NULL) = (access_token_expires_at IS NULL)
    ),
    CONSTRAINT operator_browser_session_tokens_logged_in CHECK (
        access_token IS NULL OR operator_issuer IS NOT NULL
    ),
    CONSTRAINT operator_browser_session_refresh_token_with_access_token CHECK (
        refresh_token IS NULL OR access_token IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS ix_operator_browser_session_expires_at
    ON operator_browser_session (expires_at);

-- Keep the historical marker at head on a fresh database. Existing databases were checked above and
-- keep their marker unchanged. This lets a rollback to the Python runtime see a no-op Alembic head.
-- +goose StatementBegin
DO $$
BEGIN
    IF to_regclass('alembic_version_app') IS NULL THEN
        CREATE TABLE alembic_version_app (
            version_num VARCHAR(32) NOT NULL PRIMARY KEY
        );
        INSERT INTO alembic_version_app (version_num) VALUES ('0036_auto_thread_titles');
    END IF;
END $$;
-- +goose StatementEnd
