-- Inspect only timestamp field shape and time ranges; no message body is selected.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';

SELECT json_build_object(
    'timestamp_value_types', (
        SELECT json_object_agg(value_type, n)
        FROM (
            SELECT jsonb_typeof(payload->'timestamp') AS value_type, count(*) AS n
            FROM events
            WHERE event_type = 'assistant' AND payload ? 'timestamp'
            GROUP BY jsonb_typeof(payload->'timestamp')
        ) types
    ),
    'string_timestamp_min', (
        SELECT min(payload->>'timestamp')
        FROM events
        WHERE event_type = 'assistant' AND jsonb_typeof(payload->'timestamp') = 'string'
          AND payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
    ),
    'string_timestamp_max', (
        SELECT max(payload->>'timestamp')
        FROM events
        WHERE event_type = 'assistant' AND jsonb_typeof(payload->'timestamp') = 'string'
          AND payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
    ),
    'timestamp_field_matches_created_at', (
        SELECT count(*)
        FROM events
        WHERE event_type = 'assistant' AND jsonb_typeof(payload->'timestamp') = 'string'
          AND payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
          AND (payload->>'timestamp')::timestamptz = created_at
    ),
    'timestamp_field_differs_from_created_at', (
        SELECT count(*)
        FROM events
        WHERE event_type = 'assistant' AND jsonb_typeof(payload->'timestamp') = 'string'
          AND payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
          AND (payload->>'timestamp')::timestamptz <> created_at
    ),
    'created_at_vs_payload_timestamp_seconds_min', (
        SELECT min(extract(epoch FROM created_at - (payload->>'timestamp')::timestamptz))
        FROM events
        WHERE event_type = 'assistant' AND jsonb_typeof(payload->'timestamp') = 'string'
          AND payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
    ),
    'created_at_vs_payload_timestamp_seconds_max', (
        SELECT max(extract(epoch FROM created_at - (payload->>'timestamp')::timestamptz))
        FROM events
        WHERE event_type = 'assistant' AND jsonb_typeof(payload->'timestamp') = 'string'
          AND payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
    ),
    'created_at_within_1s_of_payload_timestamp', (
        SELECT count(*)
        FROM events
        WHERE event_type = 'assistant' AND jsonb_typeof(payload->'timestamp') = 'string'
          AND payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
          AND abs(extract(epoch FROM created_at - (payload->>'timestamp')::timestamptz)) <= 1
    )
)::text;

COMMIT;
