-- Source-specific Claude Code Web token extract.
-- Execute against the claude_sessions database with psql -X -v ON_ERROR_STOP=1.
-- All history is read; events are filtered through the existing (event_type, created_at) index.
-- Only usage counters, model/time/quality metadata and session environment labels are selected.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '180s';

SELECT json_build_object(
    'record_type', 'metadata',
    'database', current_database(),
    'source', 'claude_code_web',
    'provider_attribution', 'assistant model names are mapped to anthropic only when result.modelUsage metadata explicitly labels the exact model firstParty; all other labels remain unknown',
    'extracted_at_utc', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'source_window', json_build_object(
        'first_assistant_event_utc', to_char(min(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'last_assistant_event_utc', to_char(max(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
    ),
    'sessions', (
        SELECT json_build_object(
            'count', count(*),
            'latest_listed_event_at_utc', to_char(max(last_event_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'latest_synced_event_at_utc', to_char(max(synced_last_event_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'caught_up', count(*) FILTER (WHERE synced_last_event_at >= last_event_at),
            'behind_or_missing_watermark', count(*) FILTER (WHERE synced_last_event_at IS NULL OR synced_last_event_at < last_event_at),
            'environment_kind_counts', (
                SELECT json_object_agg(environment_kind, n)
                FROM (
                    SELECT coalesce(raw->>'environment_kind', '<missing>') AS environment_kind, count(*) AS n
                    FROM sessions GROUP BY coalesce(raw->>'environment_kind', '<missing>')
                ) kinds
            )
        )::json
        FROM sessions
    ),
    'payment_lane', 'unknown',
    'payment_lane_reason', 'the Web session event schema does not classify included subscription use versus overage',
    'dedup_scope', 'session_id plus message id; message IDs recur across sessions and many have differing timestamps or output counters, so global deduplication is not supported',
    'assistant_event_rows', (SELECT count(*) FROM events WHERE event_type = 'assistant'),
    'assistant_message_rows', (SELECT count(*) FROM events WHERE event_type = 'assistant' AND jsonb_typeof(payload->'message') = 'object'),
    'assistant_message_id_missing_rows', (SELECT count(*) FROM events WHERE event_type = 'assistant' AND jsonb_typeof(payload->'message') = 'object' AND NULLIF(payload->'message'->>'id', '') IS NULL),
    'explicit_provider_models', (
        SELECT json_agg(json_build_object('model', model, 'provider_label', provider_label, 'result_rows', result_rows) ORDER BY model)
        FROM (
            SELECT model_usage.model, max(model_usage.value->>'provider') AS provider_label, count(*) AS result_rows
            FROM events e
            CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(e.payload->'modelUsage') = 'object' THEN e.payload->'modelUsage' ELSE '{}'::jsonb END) AS model_usage(model, value)
            WHERE e.event_type = 'result' AND model_usage.value->>'provider' IS NOT NULL
            GROUP BY model_usage.model
        ) provider_models
    )
)::text
FROM events
WHERE event_type = 'assistant';

WITH provider_models AS MATERIALIZED (
    SELECT model_usage.model,
           max(model_usage.value->>'provider') AS provider_label,
           count(DISTINCT model_usage.value->>'provider') AS provider_variants
    FROM events e
    CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(e.payload->'modelUsage') = 'object' THEN e.payload->'modelUsage' ELSE '{}'::jsonb END) AS model_usage(model, value)
    WHERE e.event_type = 'result'
    GROUP BY model_usage.model
), assistant_source AS MATERIALIZED (
    SELECT
        e.session_id,
        e.sequence_num,
        e.event_id,
        e.created_at,
        NULLIF(e.payload->'message'->>'id', '') AS message_id,
        e.payload->'message'->>'model' AS model,
        e.payload->'message'->>'stop_reason' AS stop_reason,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'input_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'input_tokens')::bigint END AS uncached_input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'output_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'output_tokens')::bigint END AS output_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'cache_read_input_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'cache_read_input_tokens')::bigint END AS cache_read_input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'cache_creation_input_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->>'cache_creation_input_tokens')::bigint END AS cache_write_input_tokens,
        CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'output_tokens_details'->'thinking_tokens') = 'number'
             THEN (e.payload->'message'->'usage'->'output_tokens_details'->>'thinking_tokens')::bigint END AS reasoning_output_tokens,
        coalesce(s.raw->>'environment_kind', '<missing>') AS session_environment_kind,
        e.payload->>'parent_tool_use_id' IS NOT NULL AS has_parent_tool_use_id,
        e.payload->>'agent_id' IS NOT NULL AS has_agent_id,
        NULLIF(e.payload->>'subagent_type', '') AS subagent_type,
        coalesce(e.payload->'message'->'usage'->>'service_tier', '<missing>') AS service_tier,
        coalesce(e.payload->'message'->'usage'->>'speed', '<missing>') AS speed,
        (e.payload->>'is_api_error_message' = 'true' OR e.payload->>'isApiErrorMessage' = 'true') AS is_api_error_message,
        e.payload->>'aborted' = 'true' AS is_aborted
    FROM events e
    JOIN sessions s USING (session_id)
    WHERE e.event_type = 'assistant' AND jsonb_typeof(e.payload->'message') = 'object'
), keyed_source AS MATERIALIZED (
    SELECT *,
        message_id IS NOT NULL AS message_id_present,
        CASE WHEN message_id IS NOT NULL THEN 'message:' || message_id ELSE 'event:' || event_id::text END AS request_key
    FROM assistant_source
), ordered_source AS MATERIALIZED (
    SELECT *,
        row_number() OVER (PARTITION BY session_id, request_key ORDER BY sequence_num DESC) AS reverse_sequence,
        lag(uncached_input_tokens) OVER (PARTITION BY session_id, request_key ORDER BY sequence_num) AS previous_uncached_input_tokens,
        lag(output_tokens) OVER (PARTITION BY session_id, request_key ORDER BY sequence_num) AS previous_output_tokens,
        lag(cache_read_input_tokens) OVER (PARTITION BY session_id, request_key ORDER BY sequence_num) AS previous_cache_read_input_tokens,
        lag(cache_write_input_tokens) OVER (PARTITION BY session_id, request_key ORDER BY sequence_num) AS previous_cache_write_input_tokens
    FROM keyed_source
), requests AS MATERIALIZED (
    SELECT
        session_id,
        request_key,
        bool_and(message_id_present) AS message_id_present,
        count(*) AS source_event_count,
        count(DISTINCT ordered_source.model) AS model_variants,
        count(DISTINCT uncached_input_tokens) AS input_variants,
        count(DISTINCT output_tokens) AS output_variants,
        count(DISTINCT cache_read_input_tokens) AS cache_read_variants,
        count(DISTINCT cache_write_input_tokens) AS cache_write_variants,
        bool_or(uncached_input_tokens < previous_uncached_input_tokens) AS input_decreased,
        bool_or(output_tokens < previous_output_tokens) AS output_decreased,
        bool_or(cache_read_input_tokens < previous_cache_read_input_tokens) AS cache_read_decreased,
        bool_or(cache_write_input_tokens < previous_cache_write_input_tokens) AS cache_write_decreased,
        min(uncached_input_tokens) AS uncached_input_tokens_lower,
        max(uncached_input_tokens) AS uncached_input_tokens_upper,
        max(uncached_input_tokens) FILTER (WHERE reverse_sequence = 1) AS uncached_input_tokens_last,
        min(output_tokens) AS output_tokens_lower,
        max(output_tokens) AS output_tokens_upper,
        max(output_tokens) FILTER (WHERE reverse_sequence = 1) AS output_tokens_last,
        min(cache_read_input_tokens) AS cache_read_input_tokens_lower,
        max(cache_read_input_tokens) AS cache_read_input_tokens_upper,
        max(cache_read_input_tokens) FILTER (WHERE reverse_sequence = 1) AS cache_read_input_tokens_last,
        min(cache_write_input_tokens) AS cache_write_input_tokens_lower,
        max(cache_write_input_tokens) AS cache_write_input_tokens_upper,
        max(cache_write_input_tokens) FILTER (WHERE reverse_sequence = 1) AS cache_write_input_tokens_last,
        min(reasoning_output_tokens) AS reasoning_output_tokens_lower,
        max(reasoning_output_tokens) AS reasoning_output_tokens_upper,
        max(reasoning_output_tokens) FILTER (WHERE reverse_sequence = 1) AS reasoning_output_tokens_last,
        max(ordered_source.model) FILTER (WHERE reverse_sequence = 1) AS model,
        max(stop_reason) FILTER (WHERE reverse_sequence = 1) IS NOT NULL AS has_final_stop_reason,
        max(created_at) FILTER (WHERE reverse_sequence = 1) AS final_sequence_at,
        max(session_environment_kind) FILTER (WHERE reverse_sequence = 1) AS session_environment_kind,
        max(service_tier) FILTER (WHERE reverse_sequence = 1) AS service_tier,
        max(speed) FILTER (WHERE reverse_sequence = 1) AS speed,
        max(provider_models.provider_label) FILTER (WHERE reverse_sequence = 1) AS provider_label,
        max(provider_models.provider_variants) FILTER (WHERE reverse_sequence = 1) AS provider_variants,
        max(subagent_type) FILTER (WHERE reverse_sequence = 1) AS subagent_type,
        bool_or(has_parent_tool_use_id) AS has_parent_tool_use_id,
        bool_or(has_agent_id) AS has_agent_id,
        bool_or(is_api_error_message) AS is_api_error_message,
        bool_or(is_aborted) AS is_aborted
    FROM ordered_source
    LEFT JOIN provider_models ON provider_models.model = ordered_source.model
    GROUP BY session_id, request_key
), bucketed AS MATERIALIZED (
    SELECT
        r.*,
        CASE WHEN model_variants > 1 THEN 'unknown'
             WHEN provider_label = 'firstParty' AND provider_variants <= 1 THEN 'anthropic'
             ELSE 'unknown' END AS provider,
        buckets.granularity,
        buckets.bucket_start_utc,
        CASE WHEN r.subagent_type IS NOT NULL AND lower(r.subagent_type) = 'guardian' THEN 'guardian'
             WHEN r.has_parent_tool_use_id OR r.has_agent_id OR r.subagent_type IS NOT NULL THEN 'subagent'
             ELSE 'main_or_unclassified' END AS assistant_role
    FROM requests r
    CROSS JOIN LATERAL (
        VALUES
            ('5m'::text, date_bin(interval '5 minutes', r.final_sequence_at AT TIME ZONE 'UTC', timestamp '2000-01-01 00:00:00')),
            ('hour'::text, date_trunc('hour', r.final_sequence_at AT TIME ZONE 'UTC')),
            ('week'::text, date_trunc('week', r.final_sequence_at AT TIME ZONE 'UTC'))
    ) AS buckets(granularity, bucket_start_utc)
), aggregate_rows AS (
    SELECT
        granularity,
        bucket_start_utc,
        model,
        provider,
        service_tier,
        speed,
        count(*) AS requests,
        sum(source_event_count) AS source_event_count,
        count(*) FILTER (WHERE source_event_count > 1) AS requests_with_repeated_events,
        sum(source_event_count - 1) AS repeated_event_excess,
        count(*) FILTER (WHERE input_variants > 1 OR output_variants > 1 OR cache_read_variants > 1 OR cache_write_variants > 1) AS requests_with_counter_variants,
        count(*) FILTER (WHERE input_decreased) AS requests_with_input_decrease,
        count(*) FILTER (WHERE output_decreased) AS requests_with_output_decrease,
        count(*) FILTER (WHERE cache_read_decreased) AS requests_with_cache_read_decrease,
        count(*) FILTER (WHERE cache_write_decreased) AS requests_with_cache_write_decrease,
        count(*) FILTER (WHERE NOT has_final_stop_reason) AS requests_missing_final_stop_reason,
        count(*) FILTER (WHERE is_api_error_message) AS api_error_messages,
        count(*) FILTER (WHERE is_aborted) AS aborted_messages,
        count(*) FILTER (WHERE model_variants > 1) AS model_conflict_messages,
        count(*) FILTER (WHERE NOT message_id_present) AS requests_missing_message_id,
        count(*) FILTER (WHERE assistant_role = 'guardian') AS guardian_requests,
        count(*) FILTER (WHERE assistant_role = 'subagent') AS subagent_requests,
        count(*) FILTER (WHERE assistant_role = 'main_or_unclassified') AS main_or_unclassified_requests,
        count(*) FILTER (WHERE subagent_type IS NOT NULL) AS requests_with_subagent_type,
        count(*) FILTER (WHERE has_parent_tool_use_id) AS requests_with_parent_tool_use_id,
        count(*) FILTER (WHERE has_agent_id) AS requests_with_agent_id,
        count(*) FILTER (WHERE uncached_input_tokens_last IS NULL) AS requests_missing_uncached_input,
        count(*) FILTER (WHERE output_tokens_last IS NULL) AS requests_missing_output,
        count(*) FILTER (WHERE cache_read_input_tokens_last IS NULL) AS requests_missing_cache_read,
        count(*) FILTER (WHERE cache_write_input_tokens_last IS NULL) AS requests_missing_cache_write,
        count(*) FILTER (WHERE reasoning_output_tokens_last IS NOT NULL) AS requests_with_reasoning_detail,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_last IS NULL) = 0
             THEN sum(uncached_input_tokens_last) END AS uncached_input_tokens,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_lower IS NULL) = 0
             THEN sum(uncached_input_tokens_lower) END AS uncached_input_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_upper IS NULL) = 0
             THEN sum(uncached_input_tokens_upper) END AS uncached_input_tokens_upper_bound,
        CASE WHEN count(*) FILTER (WHERE cache_read_input_tokens_last IS NULL) = 0
             THEN sum(cache_read_input_tokens_last) END AS cache_read_input_tokens,
        CASE WHEN count(*) FILTER (WHERE cache_write_input_tokens_last IS NULL) = 0
             THEN sum(cache_write_input_tokens_last) END AS cache_write_input_tokens,
        CASE WHEN count(*) FILTER (WHERE output_tokens_last IS NULL) = 0
             THEN sum(output_tokens_last) END AS output_tokens,
        CASE WHEN count(*) FILTER (WHERE output_tokens_lower IS NULL) = 0
             THEN sum(output_tokens_lower) END AS output_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE output_tokens_upper IS NULL) = 0
             THEN sum(output_tokens_upper) END AS output_tokens_upper_bound,
        sum(reasoning_output_tokens_last) AS reasoning_output_tokens_reported_sum,
        CASE WHEN count(*) FILTER (WHERE reasoning_output_tokens_last IS NULL) = 0
             THEN sum(reasoning_output_tokens_last) END AS reasoning_output_tokens,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_last IS NULL OR output_tokens_last IS NULL
                                        OR cache_read_input_tokens_last IS NULL OR cache_write_input_tokens_last IS NULL) = 0
             THEN sum(uncached_input_tokens_last + cache_read_input_tokens_last + cache_write_input_tokens_last + output_tokens_last)
        END AS total_tokens,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_lower IS NULL OR output_tokens_lower IS NULL
                                        OR cache_read_input_tokens_last IS NULL OR cache_write_input_tokens_last IS NULL) = 0
             THEN sum(uncached_input_tokens_lower + output_tokens_lower + cache_read_input_tokens_last + cache_write_input_tokens_last)
        END AS total_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_upper IS NULL OR output_tokens_upper IS NULL
                                        OR cache_read_input_tokens_last IS NULL OR cache_write_input_tokens_last IS NULL) = 0
             THEN sum(uncached_input_tokens_upper + output_tokens_upper + cache_read_input_tokens_last + cache_write_input_tokens_last)
        END AS total_tokens_upper_bound
    FROM bucketed
    GROUP BY granularity, bucket_start_utc, model, provider, service_tier, speed
)
SELECT json_build_object(
    'record_type', 'usage',
    'granularity', granularity,
    'bucket_start_utc', to_char(bucket_start_utc, 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'provider', provider,
    'model', model,
    'source', 'claude_code_web',
    'payment_lane', 'unknown',
    'requests', requests,
    'source_event_count', source_event_count,
    'requests_with_repeated_events', requests_with_repeated_events,
    'repeated_event_excess', repeated_event_excess,
    'requests_with_counter_variants', requests_with_counter_variants,
    'requests_with_input_decrease', requests_with_input_decrease,
    'requests_with_output_decrease', requests_with_output_decrease,
    'requests_with_cache_read_decrease', requests_with_cache_read_decrease,
    'requests_with_cache_write_decrease', requests_with_cache_write_decrease,
    'requests_missing_final_stop_reason', requests_missing_final_stop_reason,
    'api_error_messages', api_error_messages,
    'aborted_messages', aborted_messages,
    'model_conflict_messages', model_conflict_messages,
    'requests_missing_message_id', requests_missing_message_id,
    'guardian_requests', guardian_requests,
    'subagent_requests', subagent_requests,
    'main_or_unclassified_requests', main_or_unclassified_requests,
    'requests_with_subagent_type', requests_with_subagent_type,
    'requests_with_parent_tool_use_id', requests_with_parent_tool_use_id,
    'requests_with_agent_id', requests_with_agent_id,
    'reported_service_tier', service_tier,
    'reported_speed', speed,
    'uncached_input_tokens', uncached_input_tokens,
    'uncached_input_tokens_lower_bound', uncached_input_tokens_lower_bound,
    'uncached_input_tokens_upper_bound', uncached_input_tokens_upper_bound,
    'cache_read_input_tokens', cache_read_input_tokens,
    'cache_write_input_tokens', cache_write_input_tokens,
    'output_tokens', output_tokens,
    'output_tokens_lower_bound', output_tokens_lower_bound,
    'output_tokens_upper_bound', output_tokens_upper_bound,
    'reasoning_output_tokens', reasoning_output_tokens,
    'reasoning_output_tokens_reported_sum', reasoning_output_tokens_reported_sum,
    'requests_with_reasoning_detail', requests_with_reasoning_detail,
    'total_tokens', total_tokens,
    'total_tokens_lower_bound', total_tokens_lower_bound,
    'total_tokens_upper_bound', total_tokens_upper_bound,
    'total_tokens_definition', 'derived sum of uncached input + cache read + cache write + output; not a provider-reported total or billable amount'
)::text
FROM aggregate_rows
ORDER BY granularity, bucket_start_utc, provider, model NULLS FIRST, service_tier, speed;

COMMIT;
