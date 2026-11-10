-- Metadata-only schema inventory for the Claude Code Web session database.
-- This query never returns message text, tool content, or complete payloads.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';

SELECT json_build_object(
    'database', current_database(),
    'events_estimated_rows', (SELECT reltuples::bigint FROM pg_class WHERE oid = 'events'::regclass),
    'events_bytes', pg_total_relation_size('events'),
    'sessions_estimated_rows', (SELECT reltuples::bigint FROM pg_class WHERE oid = 'sessions'::regclass),
    'events_indexes', (SELECT json_agg(indexrelname) FROM pg_stat_user_indexes WHERE relname = 'events')
)::text;

SELECT json_build_object('table', table_name, 'columns', json_agg(column_name ORDER BY ordinal_position))::text
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name IN ('sessions', 'events')
GROUP BY table_name
ORDER BY table_name;

SELECT json_build_object('source', src, 'key', key, 'rows', n)::text
FROM (
    SELECT 'assistant.payload' AS src, k AS key, count(*) AS n
    FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload) k
    WHERE e.event_type = 'assistant'
    GROUP BY k
    UNION ALL
    SELECT 'assistant.message', k, count(*)
    FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload->'message') k
    WHERE e.event_type = 'assistant' AND jsonb_typeof(e.payload->'message') = 'object'
    GROUP BY k
    UNION ALL
    SELECT 'assistant.usage', k, count(*)
    FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload->'message'->'usage') k
    WHERE e.event_type = 'assistant' AND jsonb_typeof(e.payload->'message'->'usage') = 'object'
    GROUP BY k
    UNION ALL
    SELECT 'result.payload', k, count(*)
    FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload) k
    WHERE e.event_type = 'result'
    GROUP BY k
    UNION ALL
    SELECT 'result.usage', k, count(*)
    FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload->'usage') k
    WHERE e.event_type = 'result' AND jsonb_typeof(e.payload->'usage') = 'object'
    GROUP BY k
    UNION ALL
    SELECT 'result.modelUsage', k, count(*)
    FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload->'modelUsage') k
    WHERE e.event_type = 'result' AND jsonb_typeof(e.payload->'modelUsage') = 'object'
    GROUP BY k
    UNION ALL
    SELECT 'rate_limit.payload', k, count(*)
    FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload) k
    WHERE e.event_type = 'rate_limit_event'
    GROUP BY k
    UNION ALL
    SELECT 'session.raw', k, count(*)
    FROM sessions s CROSS JOIN LATERAL jsonb_object_keys(s.raw) k
    GROUP BY k
) x
ORDER BY src, key;

SELECT json_build_object(
    'source', 'assistant_timestamp_key_candidates',
    'payload', (
        SELECT json_agg(json_build_object('key', key, 'rows', n) ORDER BY key)
        FROM (
            SELECT k AS key, count(*) AS n
            FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload) k
            WHERE e.event_type = 'assistant' AND (k ILIKE '%time%' OR k ILIKE '%date%')
            GROUP BY k
        ) keys
    ),
    'message', (
        SELECT json_agg(json_build_object('key', key, 'rows', n) ORDER BY key)
        FROM (
            SELECT k AS key, count(*) AS n
            FROM events e CROSS JOIN LATERAL jsonb_object_keys(e.payload->'message') k
            WHERE e.event_type = 'assistant' AND jsonb_typeof(e.payload->'message') = 'object'
              AND (k ILIKE '%time%' OR k ILIKE '%date%')
            GROUP BY k
        ) keys
    )
)::text;

COMMIT;
