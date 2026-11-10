-- +goose Up
-- One baseline for an empty database or the fully upgraded v2 predecessor.
-- +goose StatementBegin
DO $app_baseline$
DECLARE
    previous_version bigint;
BEGIN
    SELECT COALESCE(max(version_id) FILTER (WHERE is_applied), 0)
      INTO previous_version
      FROM (SELECT DISTINCT ON (version_id) version_id, is_applied
              FROM agentplane_app_goose_version ORDER BY version_id, id DESC) AS latest;
    IF previous_version = 2 THEN
        IF EXISTS (SELECT 1 FROM agentplane_app_goose_version WHERE version_id > 2) OR
           (SELECT count(*) FROM (SELECT DISTINCT ON (version_id) version_id, is_applied FROM agentplane_app_goose_version ORDER BY version_id, id DESC) latest WHERE version_id IN (1, 2) AND is_applied) <> 2 OR
           to_regclass('thread') IS NULL OR to_regclass('operator_browser_session') IS NULL OR
           to_regclass('ix_operator_browser_session_expires_at') IS NULL OR
           (SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema()
               AND (table_name, column_name, data_type, is_nullable) IN (
                   ('thread', 'id', 'uuid', 'NO'), ('thread', 'name', 'text', 'YES'),
                   ('thread', 'archived', 'boolean', 'NO'), ('thread', 'created_at', 'timestamp with time zone', 'NO'),
                   ('operator_browser_session', 'id', 'text', 'NO'),
                   ('operator_browser_session', 'expires_at', 'timestamp with time zone', 'NO'),
                   ('operator_browser_session', 'payload', 'jsonb', 'NO'),
                   ('operator_browser_session', 'absolute_expires_at', 'timestamp with time zone', 'NO'),
                   ('operator_browser_session', 'operator_issuer', 'text', 'YES'),
                   ('operator_browser_session', 'operator_subject', 'text', 'YES'),
                   ('operator_browser_session', 'operator_username', 'text', 'YES'),
                   ('operator_browser_session', 'access_token', 'text', 'YES'),
                   ('operator_browser_session', 'access_token_expires_at', 'timestamp with time zone', 'YES'),
                   ('operator_browser_session', 'refresh_token', 'text', 'YES'))) <> 14 OR
           (SELECT count(*) FROM pg_constraint WHERE conrelid IN ('thread'::regclass, 'operator_browser_session'::regclass)
               AND conname IN ('thread_pkey', 'operator_browser_session_pkey', 'operator_browser_session_operator_whole',
                   'operator_browser_session_access_token_whole', 'operator_browser_session_tokens_logged_in',
                   'operator_browser_session_refresh_token_with_access_token')) <> 6 THEN
            RAISE EXCEPTION 'app version 2 schema is incomplete; repair it before baseline adoption';
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
        RETURN;
    END IF;
    IF previous_version <> 0 OR EXISTS (SELECT 1 FROM agentplane_app_goose_version WHERE version_id > 0) OR EXISTS (
        SELECT 1 FROM pg_class WHERE relnamespace = current_schema()::regnamespace AND relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        AND relname NOT IN ('agentplane_app_goose_version', 'agentplane_app_goose_version_id_seq')
    ) THEN
        RAISE EXCEPTION 'app baseline requires a fresh database or completed version 2; run the previous image first';
    END IF;
    CREATE TABLE thread (
        id uuid PRIMARY KEY, name text, archived boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL
    );
    CREATE TABLE operator_browser_session (
        id text PRIMARY KEY, expires_at timestamptz NOT NULL, payload jsonb NOT NULL,
        absolute_expires_at timestamptz NOT NULL, operator_issuer text, operator_subject text,
        operator_username text, access_token text, access_token_expires_at timestamptz, refresh_token text,
        CONSTRAINT operator_browser_session_operator_whole CHECK ((operator_issuer IS NULL) = (operator_subject IS NULL) AND (operator_issuer IS NULL) = (operator_username IS NULL)),
        CONSTRAINT operator_browser_session_access_token_whole CHECK ((access_token IS NULL) = (access_token_expires_at IS NULL)),
        CONSTRAINT operator_browser_session_tokens_logged_in CHECK (access_token IS NULL OR operator_issuer IS NOT NULL),
        CONSTRAINT operator_browser_session_refresh_token_with_access_token CHECK (refresh_token IS NULL OR access_token IS NOT NULL)
    );
    CREATE INDEX ix_operator_browser_session_expires_at ON operator_browser_session (expires_at);
END
$app_baseline$;
-- +goose StatementEnd
