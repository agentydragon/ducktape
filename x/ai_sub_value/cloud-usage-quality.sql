-- Read-only uncertainty audit for the Claude Code Web usage extract.
-- This returns aggregate token counts, model/category labels, timestamps and field
-- presence only. It never selects a prompt, message body, result text or tool output.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '180s';

WITH assistant_source AS MATERIALIZED (
    SELECT
        e.session_id,
        e.sequence_num,
        e.event_id,
        e.created_at,
        e.payload->'message'->>'id' AS message_id,
        e.payload->'message'->>'model' AS model,
        e.payload->'message'->>'stop_reason' AS stop_reason,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'input_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'input_tokens')::bigint END AS input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'output_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'output_tokens')::bigint END AS output_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'cache_read_input_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'cache_read_input_tokens')::bigint END AS cache_read_input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'cache_creation_input_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'cache_creation_input_tokens')::bigint END AS cache_write_input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'output_tokens_details'->'thinking_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->'output_tokens_details'->>'thinking_tokens')::bigint END AS thinking_tokens,
        e.payload->>'parent_tool_use_id' IS NOT NULL AS has_parent_tool_use_id,
        e.payload->>'agent_id' IS NOT NULL AS has_agent_id,
        e.payload->>'subagent_type' AS subagent_type
    FROM events e
    WHERE e.event_type = 'assistant' AND jsonb_typeof(e.payload->'message') = 'object'
), ordered_assistant AS MATERIALIZED (
    SELECT *,
        row_number() OVER (PARTITION BY session_id, message_id ORDER BY sequence_num DESC) AS reverse_sequence,
        lag(input_tokens) OVER (PARTITION BY session_id, message_id ORDER BY sequence_num) AS prior_input_tokens,
        lag(output_tokens) OVER (PARTITION BY session_id, message_id ORDER BY sequence_num) AS prior_output_tokens,
        lag(cache_read_input_tokens) OVER (PARTITION BY session_id, message_id ORDER BY sequence_num) AS prior_cache_read_input_tokens,
        lag(cache_write_input_tokens) OVER (PARTITION BY session_id, message_id ORDER BY sequence_num) AS prior_cache_write_input_tokens
    FROM assistant_source
    WHERE message_id IS NOT NULL
), assistant_messages AS MATERIALIZED (
    SELECT
        session_id,
        message_id,
        count(*) AS event_count,
        count(DISTINCT model) AS model_variants,
        count(DISTINCT input_tokens) AS input_variants,
        count(DISTINCT output_tokens) AS output_variants,
        count(DISTINCT cache_read_input_tokens) AS cache_read_variants,
        count(DISTINCT cache_write_input_tokens) AS cache_write_variants,
        bool_or(input_tokens < prior_input_tokens) AS input_decreased,
        bool_or(output_tokens < prior_output_tokens) AS output_decreased,
        bool_or(cache_read_input_tokens < prior_cache_read_input_tokens) AS cache_read_decreased,
        bool_or(cache_write_input_tokens < prior_cache_write_input_tokens) AS cache_write_decreased,
        max(input_tokens) AS input_max,
        max(output_tokens) AS output_max,
        max(input_tokens) FILTER (WHERE reverse_sequence = 1) AS input_last,
        max(output_tokens) FILTER (WHERE reverse_sequence = 1) AS output_last,
        max(cache_read_input_tokens) AS cache_read_max,
        max(cache_read_input_tokens) FILTER (WHERE reverse_sequence = 1) AS cache_read_last,
        max(cache_write_input_tokens) AS cache_write_max,
        max(cache_write_input_tokens) FILTER (WHERE reverse_sequence = 1) AS cache_write_last,
        max(thinking_tokens) FILTER (WHERE reverse_sequence = 1) AS thinking_last,
        max(model) FILTER (WHERE reverse_sequence = 1) AS model_last,
        max(stop_reason) FILTER (WHERE reverse_sequence = 1) AS stop_reason_last,
        max(created_at) FILTER (WHERE reverse_sequence = 1) AS last_sequence_at,
        max(created_at) AS max_event_time,
        bool_or(has_parent_tool_use_id) AS has_parent_tool_use_id,
        bool_or(has_agent_id) AS has_agent_id,
        bool_or(subagent_type IS NOT NULL) AS has_subagent_type
    FROM ordered_assistant
    GROUP BY session_id, message_id
), result_source AS MATERIALIZED (
    SELECT
        e.session_id,
        e.sequence_num,
        e.created_at,
        e.payload ? 'modelUsage' AS has_model_usage,
        e.payload ? 'model_usage_since_last_result' AS has_delta_marker,
        CASE WHEN jsonb_typeof(e.payload->'usage'->'input_tokens') = 'number'
             THEN (e.payload->'usage'->>'input_tokens')::bigint END AS usage_input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'usage'->'output_tokens') = 'number'
             THEN (e.payload->'usage'->>'output_tokens')::bigint END AS usage_output_tokens,
        sum(CASE WHEN jsonb_typeof(mu.value->'inputTokens') = 'number'
                 THEN (mu.value->>'inputTokens')::bigint END) AS model_usage_input_tokens,
        sum(CASE WHEN jsonb_typeof(mu.value->'outputTokens') = 'number'
                 THEN (mu.value->>'outputTokens')::bigint END) AS model_usage_output_tokens,
        sum(CASE WHEN jsonb_typeof(mu.value->'cacheReadInputTokens') = 'number'
                 THEN (mu.value->>'cacheReadInputTokens')::bigint END) AS model_usage_cache_read_tokens,
        sum(CASE WHEN jsonb_typeof(mu.value->'cacheCreationInputTokens') = 'number'
                 THEN (mu.value->>'cacheCreationInputTokens')::bigint END) AS model_usage_cache_write_tokens
    FROM events e
    LEFT JOIN LATERAL jsonb_each(
        CASE WHEN jsonb_typeof(e.payload->'modelUsage') = 'object' THEN e.payload->'modelUsage' ELSE '{}'::jsonb END
    ) AS mu(model, value) ON true
    WHERE e.event_type = 'result'
    GROUP BY e.session_id, e.sequence_num, e.created_at, e.payload
), ordered_results AS MATERIALIZED (
    SELECT *,
        lag(model_usage_input_tokens) OVER (PARTITION BY session_id ORDER BY sequence_num) AS previous_model_usage_input_tokens,
        lag(model_usage_output_tokens) OVER (PARTITION BY session_id ORDER BY sequence_num) AS previous_model_usage_output_tokens
    FROM result_source
), result_summary AS (
    SELECT json_build_object(
        'result_events', count(*),
        'result_events_with_modelUsage', count(*) FILTER (WHERE has_model_usage),
        'result_events_with_model_usage_since_last_result', count(*) FILTER (WHERE has_delta_marker),
        'usage_input_equals_modelUsage_input', count(*) FILTER (WHERE usage_input_tokens IS NOT NULL AND usage_input_tokens = model_usage_input_tokens),
        'usage_output_equals_modelUsage_output', count(*) FILTER (WHERE usage_output_tokens IS NOT NULL AND usage_output_tokens = model_usage_output_tokens),
        'modelUsage_input_decreases_vs_previous_result', count(*) FILTER (WHERE model_usage_input_tokens < previous_model_usage_input_tokens),
        'modelUsage_output_decreases_vs_previous_result', count(*) FILTER (WHERE model_usage_output_tokens < previous_model_usage_output_tokens),
        'result_usage_input_sum_over_result_records', sum(usage_input_tokens),
        'result_usage_output_sum_over_result_records', sum(usage_output_tokens),
        'result_modelUsage_input_sum_over_result_records', sum(model_usage_input_tokens),
        'result_modelUsage_output_sum_over_result_records', sum(model_usage_output_tokens),
        'result_modelUsage_cache_read_sum_over_result_records', sum(model_usage_cache_read_tokens),
        'result_modelUsage_cache_write_sum_over_result_records', sum(model_usage_cache_write_tokens),
        'first_result_at', min(created_at),
        'last_result_at', max(created_at)
    )::json AS value
    FROM ordered_results
)
SELECT json_build_object(
    'source', 'claude_code_web',
    'provider_attribution', 'model-specific; exact assistant models require firstParty metadata in result.modelUsage to map to Anthropic',
    'database', current_database(),
    'extracted_at_utc', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'sessions', (
        SELECT json_build_object(
            'count', count(*),
            'first_created_at', min(created_at),
            'latest_updated_at', max(updated_at),
            'latest_listed_event_at', max(last_event_at),
            'latest_synced_event_at', max(synced_last_event_at),
            'caught_up', count(*) FILTER (WHERE synced_last_event_at >= last_event_at),
            'behind_or_missing_watermark', count(*) FILTER (WHERE synced_last_event_at IS NULL OR synced_last_event_at < last_event_at),
            'environment_kind_counts', (
                SELECT json_object_agg(environment_kind, n)
                FROM (
                    SELECT coalesce(raw->>'environment_kind', '<missing>') AS environment_kind, count(*) AS n
                    FROM sessions GROUP BY coalesce(raw->>'environment_kind', '<missing>')
                ) kinds
            ),
            'status_counts', (
                SELECT json_object_agg(status, n)
                FROM (SELECT status, count(*) AS n FROM sessions GROUP BY status) statuses
            ),
            'parent_session_id_count', count(*) FILTER (WHERE raw->>'parent_session_id' IS NOT NULL)
        )::json
        FROM sessions
    ),
    'event_types', (
        SELECT json_object_agg(event_type, json_build_object('rows', n, 'first', first_at, 'last', last_at))
        FROM (SELECT event_type, count(*) AS n, min(created_at) AS first_at, max(created_at) AS last_at
              FROM events GROUP BY event_type) event_counts
    ),
    'assistant_usage', (
        SELECT json_build_object(
            'assistant_events_with_message_object', (SELECT count(*) FROM assistant_source),
            'message_id_null_events', (SELECT count(*) FROM assistant_source WHERE message_id IS NULL),
            'deduped_messages', count(*),
            'messages_with_repeated_events', count(*) FILTER (WHERE event_count > 1),
            'repeated_event_excess', sum(event_count - 1),
            'message_ids_reused_across_sessions', (
                SELECT count(*) FROM (SELECT message_id FROM assistant_messages GROUP BY message_id
                    HAVING count(DISTINCT session_id) > 1) reused
            ),
            'messages_with_model_conflict', count(*) FILTER (WHERE model_variants > 1),
            'messages_with_input_counter_variants', count(*) FILTER (WHERE input_variants > 1),
            'messages_with_output_counter_variants', count(*) FILTER (WHERE output_variants > 1),
            'messages_with_cache_read_counter_variants', count(*) FILTER (WHERE cache_read_variants > 1),
            'messages_with_cache_write_counter_variants', count(*) FILTER (WHERE cache_write_variants > 1),
            'messages_with_input_decrease_in_sequence', count(*) FILTER (WHERE input_decreased),
            'messages_with_output_decrease_in_sequence', count(*) FILTER (WHERE output_decreased),
            'messages_with_cache_read_decrease_in_sequence', count(*) FILTER (WHERE cache_read_decreased),
            'messages_with_cache_write_decrease_in_sequence', count(*) FILTER (WHERE cache_write_decreased),
            'messages_with_final_output_below_max', count(*) FILTER (WHERE output_last < output_max),
            'messages_missing_final_stop_reason', count(*) FILTER (WHERE stop_reason_last IS NULL),
            'messages_final_sequence_time_differs_from_max_event_time', count(*) FILTER (WHERE last_sequence_at <> max_event_time),
            'messages_with_parent_tool_use_id', count(*) FILTER (WHERE has_parent_tool_use_id),
            'messages_with_agent_id', count(*) FILTER (WHERE has_agent_id),
            'messages_with_subagent_type', count(*) FILTER (WHERE has_subagent_type),
            'input_tokens_at_last_sequence', sum(input_last),
            'input_tokens_max_observed', sum(input_max),
            'output_tokens_at_last_sequence', sum(output_last),
            'output_tokens_max_observed', sum(output_max),
            'cache_read_tokens_at_last_sequence', sum(cache_read_last),
            'cache_read_tokens_max_observed', sum(cache_read_max),
            'cache_write_tokens_at_last_sequence', sum(cache_write_last),
            'cache_write_tokens_max_observed', sum(cache_write_max),
            'thinking_tokens_reported_message_count', count(*) FILTER (WHERE thinking_last IS NOT NULL),
            'thinking_tokens_at_last_sequence', sum(thinking_last),
            'first_last_sequence_at', min(last_sequence_at),
            'last_last_sequence_at', max(last_sequence_at)
        )::json
        FROM assistant_messages
    ),
    'messages_by_exact_model', (
        SELECT json_agg(json_build_object(
            'model', model_last,
            'messages', n,
            'messages_with_model_conflict', model_conflicts,
            'input_tokens_at_last_sequence', input_tokens,
            'output_tokens_at_last_sequence', output_tokens,
            'cache_read_tokens_at_last_sequence', cache_read_tokens,
            'cache_write_tokens_at_last_sequence', cache_write_tokens
        ) ORDER BY n DESC, model_last)
        FROM (
            SELECT model_last, count(*) AS n,
                   count(*) FILTER (WHERE model_variants > 1) AS model_conflicts,
                   sum(input_last) AS input_tokens,
                   sum(output_last) AS output_tokens,
                   sum(cache_read_last) AS cache_read_tokens,
                   sum(cache_write_last) AS cache_write_tokens
            FROM assistant_messages GROUP BY model_last
        ) model_counts
    ),
    'results_reconciliation_only', (SELECT value FROM result_summary),
    'reasoning_usage_detail', (
        SELECT json_build_object(
            'assistant_messages_with_thinking_tokens', count(*) FILTER (WHERE thinking_last IS NOT NULL),
            'thinking_tokens_sum', sum(thinking_last),
            'thinking_tokens_over_output_messages', count(*) FILTER (WHERE thinking_last > output_last),
            'thinking_tokens_equal_output_messages', count(*) FILTER (WHERE thinking_last = output_last)
        )::json
        FROM assistant_messages
    ),
    'rate_limit_status_counts', (
        SELECT json_object_agg(coalesce(status, '<missing>'), n)
        FROM (SELECT payload->'rate_limit_info'->>'status' AS status, count(*) AS n
              FROM events WHERE event_type = 'rate_limit_event'
              GROUP BY payload->'rate_limit_info'->>'status') statuses
    ),
    'reported_service_tier_counts', (
        SELECT json_object_agg(coalesce(service_tier, '<missing>'), n)
        FROM (SELECT payload->'message'->'usage'->>'service_tier' AS service_tier, count(*) AS n
              FROM events WHERE event_type = 'assistant'
              GROUP BY payload->'message'->'usage'->>'service_tier') tiers
    ),
    'reported_speed_counts', (
        SELECT json_object_agg(coalesce(speed, '<missing>'), n)
        FROM (SELECT payload->'message'->'usage'->>'speed' AS speed, count(*) AS n
              FROM events WHERE event_type = 'assistant'
              GROUP BY payload->'message'->'usage'->>'speed') speeds
    ),
    'billing_scope', json_build_object(
        'product_surface', 'claude_code_web',
        'payment_lane', 'unknown',
        'reason', 'the session and usage event schema has no verified included-versus-overage field',
        'result_usage_fallback_credit_non_null', (
            SELECT count(*) FROM events WHERE event_type = 'result'
              AND jsonb_typeof(payload->'usage'->'fallback_credit') NOT IN ('null', 'unknown')
        ),
        'result_modelUsage_cost_basis_counts', (
            SELECT json_object_agg(coalesce(cost_basis, '<missing>'), n)
            FROM (
                SELECT model_usage.value->>'costBasis' AS cost_basis, count(*) AS n
                FROM events e
                CROSS JOIN LATERAL jsonb_each(
                    CASE WHEN jsonb_typeof(e.payload->'modelUsage') = 'object' THEN e.payload->'modelUsage' ELSE '{}'::jsonb END
                ) AS model_usage(model, value)
                WHERE e.event_type = 'result'
                GROUP BY model_usage.value->>'costBasis'
            ) cost_bases
        )
    )
)::text;

COMMIT;
