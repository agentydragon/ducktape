-- Read-only counter-semantic audit. Only numeric counters, model labels, timestamps,
-- IDs and quality counts are returned; message/tool content is never selected.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '90s';

WITH assistant_rows AS (
    SELECT
        session_id,
        sequence_num,
        created_at,
        payload->'message'->>'id' AS message_id,
        payload->'message'->>'model' AS model,
        CASE WHEN jsonb_typeof(payload->'message'->'usage'->'input_tokens') = 'number'
             THEN (payload->'message'->'usage'->>'input_tokens')::bigint END AS input_tokens,
        CASE WHEN jsonb_typeof(payload->'message'->'usage'->'output_tokens') = 'number'
             THEN (payload->'message'->'usage'->>'output_tokens')::bigint END AS output_tokens,
        CASE WHEN jsonb_typeof(payload->'message'->'usage'->'cache_read_input_tokens') = 'number'
             THEN (payload->'message'->'usage'->>'cache_read_input_tokens')::bigint END AS cache_read_input_tokens,
        CASE WHEN jsonb_typeof(payload->'message'->'usage'->'cache_creation_input_tokens') = 'number'
             THEN (payload->'message'->'usage'->>'cache_creation_input_tokens')::bigint END AS cache_creation_input_tokens,
        payload->>'parent_tool_use_id' AS parent_tool_use_id,
        payload->>'agent_id' AS agent_id,
        payload->>'subagent_type' AS subagent_type
    FROM events
    WHERE event_type = 'assistant' AND jsonb_typeof(payload->'message') = 'object'
), message_rows AS (
    SELECT
        session_id,
        message_id,
        count(*) AS event_count,
        count(DISTINCT model) AS model_variants,
        min(model) AS model,
        min(created_at) AS first_event_at,
        max(created_at) AS last_event_at,
        min(sequence_num) AS first_sequence,
        max(sequence_num) AS last_sequence,
        count(DISTINCT input_tokens) AS input_variants,
        count(DISTINCT output_tokens) AS output_variants,
        count(DISTINCT cache_read_input_tokens) AS cache_read_variants,
        count(DISTINCT cache_creation_input_tokens) AS cache_creation_variants,
        max(input_tokens) AS input_tokens,
        max(output_tokens) AS output_tokens,
        max(cache_read_input_tokens) AS cache_read_input_tokens,
        max(cache_creation_input_tokens) AS cache_creation_input_tokens,
        bool_or(parent_tool_use_id IS NOT NULL) AS has_parent_tool_use_id,
        bool_or(agent_id IS NOT NULL) AS has_agent_id,
        bool_or(subagent_type IS NOT NULL) AS has_subagent_type
    FROM assistant_rows
    WHERE message_id IS NOT NULL
    GROUP BY session_id, message_id
), assistant_summary AS (
    SELECT json_build_object(
        'assistant_events', (SELECT count(*) FROM assistant_rows),
        'message_id_null_events', (SELECT count(*) FROM assistant_rows WHERE message_id IS NULL),
        'messages', count(*),
        'messages_with_repeated_events', count(*) FILTER (WHERE event_count > 1),
        'repeated_event_excess', sum(event_count - 1),
        'messages_with_model_conflict', count(*) FILTER (WHERE model_variants > 1),
        'messages_with_input_counter_variants', count(*) FILTER (WHERE input_variants > 1),
        'messages_with_output_counter_variants', count(*) FILTER (WHERE output_variants > 1),
        'messages_with_cache_read_variants', count(*) FILTER (WHERE cache_read_variants > 1),
        'messages_with_cache_write_variants', count(*) FILTER (WHERE cache_creation_variants > 1),
        'messages_with_parent_tool_use_id', count(*) FILTER (WHERE has_parent_tool_use_id),
        'messages_with_agent_id', count(*) FILTER (WHERE has_agent_id),
        'messages_with_subagent_type', count(*) FILTER (WHERE has_subagent_type),
        'sum_max_input_tokens', sum(input_tokens),
        'sum_max_output_tokens', sum(output_tokens),
        'sum_max_cache_read_input_tokens', sum(cache_read_input_tokens),
        'sum_max_cache_creation_input_tokens', sum(cache_creation_input_tokens),
        'model_count', count(DISTINCT model),
        'first_event_at', min(first_event_at),
        'last_event_at', max(last_event_at)
    )::text AS value
    FROM message_rows
)
SELECT value FROM assistant_summary;

SELECT json_build_object('category', category, 'json_type', json_type, 'rows', n)::text
FROM (
    SELECT 'message.usage.' || k AS category, jsonb_typeof(v) AS json_type, count(*) AS n
    FROM events e
    CROSS JOIN LATERAL jsonb_each(e.payload->'message'->'usage') AS kv(k, v)
    WHERE e.event_type = 'assistant' AND jsonb_typeof(e.payload->'message'->'usage') = 'object'
    GROUP BY k, jsonb_typeof(v)
    UNION ALL
    SELECT 'result.usage.' || k, jsonb_typeof(v), count(*)
    FROM events e
    CROSS JOIN LATERAL jsonb_each(e.payload->'usage') AS kv(k, v)
    WHERE e.event_type = 'result' AND jsonb_typeof(e.payload->'usage') = 'object'
    GROUP BY k, jsonb_typeof(v)
    UNION ALL
    SELECT 'result.modelUsage.' || model || '.' || k, jsonb_typeof(v), count(*)
    FROM events e
    CROSS JOIN LATERAL jsonb_each(e.payload->'modelUsage') AS model_entry(model, value)
    CROSS JOIN LATERAL jsonb_each(model_entry.value) AS usage_entry(k, v)
    WHERE e.event_type = 'result'
      AND jsonb_typeof(e.payload->'modelUsage') = 'object'
      AND jsonb_typeof(model_entry.value) = 'object'
    GROUP BY model, k, jsonb_typeof(v)
) x
ORDER BY category, json_type;

WITH result_rows AS (
    SELECT
        e.session_id,
        e.sequence_num,
        e.created_at,
        e.payload ? 'usage' AS has_usage,
        e.payload ? 'modelUsage' AS has_model_usage,
        e.payload ? 'model_usage_since_last_result' AS has_delta_marker,
        CASE WHEN jsonb_typeof(e.payload->'usage'->'input_tokens') = 'number'
             THEN (e.payload->'usage'->>'input_tokens')::bigint END AS usage_input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'usage'->'output_tokens') = 'number'
             THEN (e.payload->'usage'->>'output_tokens')::bigint END AS usage_output_tokens,
        sum(CASE WHEN jsonb_typeof(model_usage.value->'inputTokens') = 'number'
                 THEN (model_usage.value->>'inputTokens')::bigint END) AS model_usage_input_tokens,
        sum(CASE WHEN jsonb_typeof(model_usage.value->'outputTokens') = 'number'
                 THEN (model_usage.value->>'outputTokens')::bigint END) AS model_usage_output_tokens,
        sum(CASE WHEN jsonb_typeof(model_usage.value->'cacheReadInputTokens') = 'number'
                 THEN (model_usage.value->>'cacheReadInputTokens')::bigint END) AS model_usage_cache_read_input_tokens,
        sum(CASE WHEN jsonb_typeof(model_usage.value->'cacheCreationInputTokens') = 'number'
                 THEN (model_usage.value->>'cacheCreationInputTokens')::bigint END) AS model_usage_cache_creation_input_tokens
    FROM events e
    LEFT JOIN LATERAL jsonb_each(e.payload->'modelUsage') AS model_usage(model, value)
        ON jsonb_typeof(e.payload->'modelUsage') = 'object'
    WHERE e.event_type = 'result'
    GROUP BY e.session_id, e.sequence_num, e.created_at, e.payload
), sequenced_result_rows AS (
    SELECT *,
        lag(model_usage_input_tokens) OVER (PARTITION BY session_id ORDER BY sequence_num) AS prior_model_usage_input_tokens,
        lag(model_usage_output_tokens) OVER (PARTITION BY session_id ORDER BY sequence_num) AS prior_model_usage_output_tokens
    FROM result_rows
)
SELECT json_build_object(
    'result_events', count(*),
    'with_usage', count(*) FILTER (WHERE has_usage),
    'with_modelUsage', count(*) FILTER (WHERE has_model_usage),
    'with_model_usage_since_last_result', count(*) FILTER (WHERE has_delta_marker),
    'usage_input_equals_modelUsage_input', count(*) FILTER (WHERE usage_input_tokens IS NOT NULL AND usage_input_tokens = model_usage_input_tokens),
    'usage_output_equals_modelUsage_output', count(*) FILTER (WHERE usage_output_tokens IS NOT NULL AND usage_output_tokens = model_usage_output_tokens),
    'modelUsage_input_decreases_from_prior_result', count(*) FILTER (WHERE model_usage_input_tokens < prior_model_usage_input_tokens),
    'modelUsage_output_decreases_from_prior_result', count(*) FILTER (WHERE model_usage_output_tokens < prior_model_usage_output_tokens),
    'modelUsage_input_rows_with_prior', count(*) FILTER (WHERE model_usage_input_tokens IS NOT NULL AND prior_model_usage_input_tokens IS NOT NULL),
    'modelUsage_output_rows_with_prior', count(*) FILTER (WHERE model_usage_output_tokens IS NOT NULL AND prior_model_usage_output_tokens IS NOT NULL),
    'result_usage_input_sum_over_result_records', sum(usage_input_tokens),
    'result_usage_output_sum_over_result_records', sum(usage_output_tokens),
    'result_modelUsage_input_sum_over_result_records', sum(model_usage_input_tokens),
    'result_modelUsage_output_sum_over_result_records', sum(model_usage_output_tokens),
    'first_result_at', min(created_at),
    'last_result_at', max(created_at)
)::text
FROM sequenced_result_rows;

SELECT json_build_object(
    'sessions', count(*),
    'first_session_created_at', min(created_at),
    'last_session_updated_at', max(updated_at),
    'last_listed_event_at', max(last_event_at),
    'caught_up', count(*) FILTER (WHERE synced_last_event_at >= last_event_at),
    'uncaught_up_or_missing_watermark', count(*) FILTER (WHERE synced_last_event_at IS NULL OR synced_last_event_at < last_event_at),
    'environment_kind_counts', (SELECT json_object_agg(environment_kind, n)
        FROM (SELECT coalesce(raw->>'environment_kind', '<missing>') AS environment_kind, count(*) AS n
              FROM sessions GROUP BY coalesce(raw->>'environment_kind', '<missing>')) kinds),
    'parent_session_id_count', count(*) FILTER (WHERE raw->>'parent_session_id' IS NOT NULL)
)::text
FROM sessions;

SELECT json_build_object('model', model, 'messages', count(*), 'events', sum(event_count),
                         'input_tokens', sum(input_tokens), 'output_tokens', sum(output_tokens),
                         'cache_read_input_tokens', sum(cache_read_input_tokens),
                         'cache_creation_input_tokens', sum(cache_creation_input_tokens),
                         'first', min(first_event_at), 'last', max(last_event_at))::text
FROM (
    SELECT session_id, message_id, min(model) AS model, count(*) AS event_count,
           max(input_tokens) AS input_tokens, max(output_tokens) AS output_tokens,
           max(cache_read_input_tokens) AS cache_read_input_tokens,
           max(cache_creation_input_tokens) AS cache_creation_input_tokens,
           min(created_at) AS first_event_at, max(created_at) AS last_event_at
    FROM (
        SELECT session_id, payload->'message'->>'id' AS message_id, created_at,
               payload->'message'->>'model' AS model,
               CASE WHEN jsonb_typeof(payload->'message'->'usage'->'input_tokens') = 'number'
                    THEN (payload->'message'->'usage'->>'input_tokens')::bigint END AS input_tokens,
               CASE WHEN jsonb_typeof(payload->'message'->'usage'->'output_tokens') = 'number'
                    THEN (payload->'message'->'usage'->>'output_tokens')::bigint END AS output_tokens,
               CASE WHEN jsonb_typeof(payload->'message'->'usage'->'cache_read_input_tokens') = 'number'
                    THEN (payload->'message'->'usage'->>'cache_read_input_tokens')::bigint END AS cache_read_input_tokens,
               CASE WHEN jsonb_typeof(payload->'message'->'usage'->'cache_creation_input_tokens') = 'number'
                    THEN (payload->'message'->'usage'->>'cache_creation_input_tokens')::bigint END AS cache_creation_input_tokens
        FROM events WHERE event_type = 'assistant' AND jsonb_typeof(payload->'message') = 'object'
    ) rows
    WHERE message_id IS NOT NULL
    GROUP BY session_id, message_id
) messages
GROUP BY model
ORDER BY count(*) DESC, model;

COMMIT;
