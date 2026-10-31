-- At most the first 2,000 physical records from the newest session per machine/harness.
-- This is a bounded schema sample, not a completeness or usage-total estimate.
-- Reconstruct inherited prefixes once: the latest version stores only its divergent suffix.
-- The normalized usage_observations table is the only source of token/model fields here.
WITH RECURSIVE
ranked AS (
    SELECT *, row_number() OVER (
        PARTITION BY machine_id, harness ORDER BY last_activity_at DESC NULLS LAST
    ) AS rank
    FROM sessions
),
chosen AS (
    SELECT s.machine_id, s.harness, s.id AS session_id,
           f.id, f.parent_version_id, f.shared_prefix_record_count, f.record_count,
           f.complete AS version_complete, f.parse_issue_count
    FROM ranked s
    CROSS JOIN LATERAL (
        SELECT * FROM file_versions
        WHERE session_id = s.id
        ORDER BY last_seen_at DESC, first_seen_at DESC, id DESC LIMIT 1
    ) f
    WHERE s.rank = 1
),
chain AS (
    SELECT machine_id, harness, session_id, id, parent_version_id,
           shared_prefix_record_count, record_count AS upper_ordinal,
           version_complete, parse_issue_count
    FROM chosen
    UNION ALL
    SELECT c.machine_id, c.harness, c.session_id, p.id, p.parent_version_id,
           p.shared_prefix_record_count,
           least(c.upper_ordinal, c.shared_prefix_record_count),
           c.version_complete, c.parse_issue_count
    FROM chain c
    JOIN file_versions p ON p.id = c.parent_version_id
)
SELECT json_build_object(
    'machine', c.machine_id,
    'harness', c.harness,
    'session', md5(c.session_id::text),
    'ordinal', r.ordinal,
    'version_status', CASE WHEN c.version_complete THEN 'complete' ELSE 'partial' END,
    'version_parse_issue_count', c.parse_issue_count,
    'timestamp', p.timestamp,
    'kind', p.kind,
    'parse_error', p.parse_error,
    'parser_version', p.parser_version,
    'payload', md5(p.id::text),
    'observations', coalesce(observations.rows, '[]'::json)
) AS sample
FROM chain c
CROSS JOIN LATERAL (
    SELECT * FROM file_version_records
    WHERE file_version_id = c.id AND ordinal < least(c.upper_ordinal, 2000)
    ORDER BY ordinal LIMIT 2000
) r
CROSS JOIN LATERAL (
    SELECT id, parser_version, timestamp, kind, parse_error
    FROM record_payloads WHERE id = r.payload_id OFFSET 0
) p
LEFT JOIN LATERAL (
    SELECT json_agg(json_build_object(
        'observation_index', o.observation_index,
        'source_field', o.source_field,
        'counter_kind', o.counter_kind,
        'model', o.model,
        'response', md5(o.response_id),
        'message', md5(o.message_id),
        'turn', md5(o.turn_id),
        'input_tokens', o.input_tokens,
        'output_tokens', o.output_tokens,
        'cache_read_input_tokens', o.cache_read_input_tokens,
        'cache_write_input_tokens', o.cache_write_input_tokens,
        'reasoning_output_tokens', o.reasoning_output_tokens,
        'total_tokens', o.total_tokens
    ) ORDER BY o.observation_index) AS rows
    FROM usage_observations o
    WHERE o.payload_id = p.id
) observations ON TRUE
WHERE p.kind IN ('assistant', 'turn_context', 'token_usage_record', 'event_msg')
   OR p.parse_error IS NOT NULL
   OR EXISTS (
       SELECT 1 FROM usage_observations o WHERE o.payload_id = p.id
   )
ORDER BY c.machine_id, c.harness, c.session_id, r.ordinal;
