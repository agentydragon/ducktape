-- Example request-grain audit, not a bill. MAX of streaming usage components
-- prevents repeated content blocks multiplying the same message's counters.
-- Compare with result.modelUsage deltas before treating output counts as final.
WITH per_message AS (
    SELECT session_id, payload->'message'->>'id' AS message_id,
           payload->'message'->>'model' AS model, count(*) AS n,
           max((payload->'message'->'usage'->>'input_tokens')::bigint) AS input,
           max((payload->'message'->'usage'->>'output_tokens')::bigint) AS output,
           max((payload->'message'->'usage'->>'cache_read_input_tokens')::bigint) AS cached,
           max((payload->'message'->'usage'->>'cache_creation_input_tokens')::bigint) AS written,
           min(created_at) AS t
    FROM events
    WHERE event_type = 'assistant' AND created_at >= now() - interval '30 days'
      AND payload->'message'->>'id' IS NOT NULL
    GROUP BY session_id, message_id, model
)
SELECT json_build_object(
    'model', model, 'messages', count(*), 'events', sum(n),
    'missing_usage', count(*) FILTER (WHERE input IS NULL),
    'input', sum(input), 'output', sum(output),
    'cache_read', sum(cached), 'cache_write', sum(written),
    'first', min(t), 'last', max(t))
FROM per_message
GROUP BY model
ORDER BY count(*) DESC;
