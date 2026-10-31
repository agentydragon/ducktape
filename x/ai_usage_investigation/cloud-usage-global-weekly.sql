-- All-history global-message-ID lower scenario for Claude Code Web accounting.
-- Only usage counters, exact model/provider labels, UTC timestamps and quality
-- metadata are selected. No prompts, message text, result text or tool content.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '180s';

SELECT json_build_object(
    'record_type', 'metadata',
    'database', current_database(),
    'source', 'claude_code_web',
    'extracted_at_utc', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'source_window', json_build_object(
        'first_assistant_event_utc', to_char(min(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'last_assistant_event_utc', to_char(max(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
    ),
    'session_id_count', (SELECT count(*) FROM sessions),
    'deduplication_scenario', 'message IDs deduplicated globally across sessions; 133 missing-message-ID events remain individually scoped to session and event ID',
    'weekly_time_assignment', 'earliest observed final-sequence events.created_at among session copies; copied message IDs can have different archive timestamps, so week placement is uncertain',
    'token_estimate', 'global-ID point estimate: minimum last-sequence counter across session copies per message ID; lower/upper bounds retain min/max over all streamed events and copies',
    'payment_lane', 'unknown',
    'payment_lane_reason', 'session events do not distinguish included subscription use from overage or extra usage'
)::text
FROM events
WHERE event_type = 'assistant';

WITH provider_models AS MATERIALIZED (
    SELECT model_usage.model,
           max(model_usage.value->>'provider') AS provider_label,
           count(DISTINCT model_usage.value->>'provider') AS provider_variants
    FROM events e
    CROSS JOIN LATERAL jsonb_each(
        CASE WHEN jsonb_typeof(e.payload->'modelUsage') = 'object' THEN e.payload->'modelUsage' ELSE '{}'::jsonb END
    ) AS model_usage(model, value)
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
        e.payload->>'aborted' = 'true' AS is_aborted,
        pm.provider_label,
        pm.provider_variants
    FROM events e
    JOIN sessions s USING (session_id)
    LEFT JOIN provider_models pm ON pm.model = e.payload->'message'->>'model'
    WHERE e.event_type = 'assistant' AND jsonb_typeof(e.payload->'message') = 'object'
), keyed_source AS MATERIALIZED (
    SELECT *,
        message_id IS NOT NULL AS message_id_present,
        CASE WHEN message_id IS NOT NULL THEN 'message:' || message_id
             ELSE 'event:' || session_id || ':' || event_id::text END AS global_request_key
    FROM assistant_source
), ordered_source AS MATERIALIZED (
    SELECT *,
        row_number() OVER (PARTITION BY session_id, global_request_key ORDER BY sequence_num DESC) AS reverse_sequence,
        lag(uncached_input_tokens) OVER (PARTITION BY session_id, global_request_key ORDER BY sequence_num) AS previous_uncached_input_tokens,
        lag(output_tokens) OVER (PARTITION BY session_id, global_request_key ORDER BY sequence_num) AS previous_output_tokens,
        lag(cache_read_input_tokens) OVER (PARTITION BY session_id, global_request_key ORDER BY sequence_num) AS previous_cache_read_input_tokens,
        lag(cache_write_input_tokens) OVER (PARTITION BY session_id, global_request_key ORDER BY sequence_num) AS previous_cache_write_input_tokens
    FROM keyed_source
), per_session_requests AS MATERIALIZED (
    SELECT
        session_id,
        global_request_key,
        bool_and(message_id_present) AS message_id_present,
        count(*) AS source_event_count,
        count(DISTINCT model) AS model_variants,
        count(DISTINCT uncached_input_tokens) AS input_variants,
        count(DISTINCT output_tokens) AS output_variants,
        count(DISTINCT cache_read_input_tokens) AS cache_read_variants,
        count(DISTINCT cache_write_input_tokens) AS cache_write_variants,
        bool_or(output_tokens < previous_output_tokens) AS output_decreased,
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
        max(reasoning_output_tokens) FILTER (WHERE reverse_sequence = 1) AS reasoning_output_tokens_last,
        max(model) FILTER (WHERE reverse_sequence = 1) AS model,
        max(provider_label) FILTER (WHERE reverse_sequence = 1) AS provider_label,
        max(provider_variants) FILTER (WHERE reverse_sequence = 1) AS provider_variants,
        max(service_tier) FILTER (WHERE reverse_sequence = 1) AS service_tier,
        max(speed) FILTER (WHERE reverse_sequence = 1) AS speed,
        max(created_at) FILTER (WHERE reverse_sequence = 1) AS final_sequence_at,
        max(stop_reason) FILTER (WHERE reverse_sequence = 1) IS NOT NULL AS has_final_stop_reason,
        bool_or(is_api_error_message) AS is_api_error_message,
        bool_or(is_aborted) AS is_aborted,
        bool_or(has_parent_tool_use_id) AS has_parent_tool_use_id,
        bool_or(has_agent_id) AS has_agent_id,
        bool_or(subagent_type IS NOT NULL) AS has_subagent_type,
        bool_or(subagent_type IS NOT NULL AND lower(subagent_type) = 'guardian') AS is_guardian
    FROM ordered_source
    GROUP BY session_id, global_request_key
), global_requests AS MATERIALIZED (
    SELECT
        global_request_key,
        count(*) AS session_copies,
        sum(source_event_count) AS source_event_count,
        bool_and(message_id_present) AS message_id_present,
        count(DISTINCT model) AS final_model_variants,
        bool_or(model_variants > 1) AS any_within_session_model_conflict,
        bool_or(input_variants > 1 OR output_variants > 1 OR cache_read_variants > 1 OR cache_write_variants > 1) AS any_counter_variants,
        bool_or(output_decreased) AS any_output_decrease,
        min(model) AS model,
        max(provider_label) AS provider_label,
        count(DISTINCT provider_label) AS provider_label_variants,
        max(provider_variants) AS provider_variants,
        max(service_tier) AS service_tier,
        max(speed) AS speed,
        min(final_sequence_at) AS earliest_final_sequence_at,
        max(final_sequence_at) AS latest_final_sequence_at,
        min(uncached_input_tokens_lower) AS uncached_input_tokens_lower,
        max(uncached_input_tokens_upper) AS uncached_input_tokens_upper,
        min(uncached_input_tokens_last) AS uncached_input_tokens_global_min_last,
        max(uncached_input_tokens_last) AS uncached_input_tokens_global_max_last,
        min(output_tokens_lower) AS output_tokens_lower,
        max(output_tokens_upper) AS output_tokens_upper,
        min(cache_read_input_tokens_lower) AS cache_read_input_tokens_lower,
        max(cache_read_input_tokens_upper) AS cache_read_input_tokens_upper,
        min(cache_read_input_tokens_last) AS cache_read_input_tokens_global_min_last,
        max(cache_read_input_tokens_last) AS cache_read_input_tokens_global_max_last,
        min(cache_write_input_tokens_lower) AS cache_write_input_tokens_lower,
        max(cache_write_input_tokens_upper) AS cache_write_input_tokens_upper,
        min(cache_write_input_tokens_last) AS cache_write_input_tokens_global_min_last,
        max(cache_write_input_tokens_last) AS cache_write_input_tokens_global_max_last,
        min(reasoning_output_tokens_last) AS reasoning_output_tokens_lower,
        max(reasoning_output_tokens_last) AS reasoning_output_tokens_upper,
        min(output_tokens_last) AS output_tokens_global_min_last,
        max(output_tokens_last) AS output_tokens_global_max_last,
        count(*) FILTER (WHERE NOT has_final_stop_reason) AS session_copies_missing_stop_reason,
        count(*) FILTER (WHERE is_api_error_message) AS session_copies_api_error,
        count(*) FILTER (WHERE is_aborted) AS session_copies_aborted,
        bool_or(has_parent_tool_use_id OR has_agent_id OR has_subagent_type) AS has_subagent_role,
        bool_or(is_guardian) AS has_guardian_role
    FROM per_session_requests
    GROUP BY global_request_key
), bucketed AS MATERIALIZED (
    SELECT
        g.*,
        CASE WHEN final_model_variants > 1 OR any_within_session_model_conflict THEN NULL ELSE model END AS exact_model,
        CASE WHEN final_model_variants = 1 AND NOT any_within_session_model_conflict
                   AND provider_label = 'firstParty' AND provider_label_variants <= 1 AND provider_variants <= 1
             THEN 'anthropic' ELSE 'unknown' END AS provider,
        date_trunc('week', earliest_final_sequence_at AT TIME ZONE 'UTC') AS bucket_start_utc
    FROM global_requests g
), weekly AS MATERIALIZED (
    SELECT
        bucket_start_utc,
        provider,
        exact_model,
        service_tier,
        speed,
        count(*) AS requests,
        sum(session_copies) AS session_copy_records,
        sum(session_copies - 1) AS session_copy_excess,
        count(*) FILTER (WHERE session_copies > 1) AS requests_with_cross_session_copies,
        count(*) FILTER (WHERE date_trunc('week', earliest_final_sequence_at AT TIME ZONE 'UTC')
                               <> date_trunc('week', latest_final_sequence_at AT TIME ZONE 'UTC')) AS requests_whose_copies_cross_utc_week,
        max(extract(epoch FROM latest_final_sequence_at - earliest_final_sequence_at)) AS max_copy_time_span_seconds,
        count(*) FILTER (WHERE any_counter_variants) AS requests_with_counter_variants,
        count(*) FILTER (WHERE any_output_decrease) AS requests_with_output_decrease,
        count(*) FILTER (WHERE session_copies_missing_stop_reason > 0) AS requests_with_missing_stop_reason_copy,
        count(*) FILTER (WHERE session_copies_api_error > 0) AS api_error_messages,
        count(*) FILTER (WHERE session_copies_aborted > 0) AS aborted_messages,
        count(*) FILTER (WHERE NOT message_id_present) AS requests_missing_message_id,
        count(*) FILTER (WHERE has_subagent_role) AS requests_with_subagent_role,
        count(*) FILTER (WHERE has_guardian_role) AS guardian_requests,
        count(*) FILTER (WHERE uncached_input_tokens_lower IS NULL) AS requests_missing_uncached_input,
        count(*) FILTER (WHERE output_tokens_lower IS NULL) AS requests_missing_output,
        count(*) FILTER (WHERE cache_read_input_tokens_lower IS NULL) AS requests_missing_cache_read,
        count(*) FILTER (WHERE cache_write_input_tokens_lower IS NULL) AS requests_missing_cache_write,
        count(*) FILTER (WHERE reasoning_output_tokens_lower IS NOT NULL) AS requests_with_reasoning_detail,
        sum(reasoning_output_tokens_lower) AS reasoning_output_tokens_reported_sum,
        CASE WHEN count(*) FILTER (WHERE reasoning_output_tokens_lower IS NULL) = 0
             THEN sum(reasoning_output_tokens_lower) END AS reasoning_output_tokens,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_global_min_last IS NULL) = 0 THEN sum(uncached_input_tokens_global_min_last) END AS uncached_input_tokens,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_lower IS NULL) = 0 THEN sum(uncached_input_tokens_lower) END AS uncached_input_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_upper IS NULL) = 0 THEN sum(uncached_input_tokens_upper) END AS uncached_input_tokens_upper_bound,
        CASE WHEN count(*) FILTER (WHERE cache_read_input_tokens_global_min_last IS NULL) = 0 THEN sum(cache_read_input_tokens_global_min_last) END AS cache_read_input_tokens,
        CASE WHEN count(*) FILTER (WHERE cache_read_input_tokens_lower IS NULL) = 0 THEN sum(cache_read_input_tokens_lower) END AS cache_read_input_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE cache_read_input_tokens_upper IS NULL) = 0 THEN sum(cache_read_input_tokens_upper) END AS cache_read_input_tokens_upper_bound,
        CASE WHEN count(*) FILTER (WHERE cache_write_input_tokens_global_min_last IS NULL) = 0 THEN sum(cache_write_input_tokens_global_min_last) END AS cache_write_input_tokens,
        CASE WHEN count(*) FILTER (WHERE cache_write_input_tokens_lower IS NULL) = 0 THEN sum(cache_write_input_tokens_lower) END AS cache_write_input_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE cache_write_input_tokens_upper IS NULL) = 0 THEN sum(cache_write_input_tokens_upper) END AS cache_write_input_tokens_upper_bound,
        CASE WHEN count(*) FILTER (WHERE output_tokens_global_min_last IS NULL) = 0 THEN sum(output_tokens_global_min_last) END AS output_tokens,
        CASE WHEN count(*) FILTER (WHERE output_tokens_lower IS NULL) = 0 THEN sum(output_tokens_lower) END AS output_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE output_tokens_upper IS NULL) = 0 THEN sum(output_tokens_upper) END AS output_tokens_upper_bound,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_global_min_last IS NULL OR output_tokens_global_min_last IS NULL
                                        OR cache_read_input_tokens_global_min_last IS NULL OR cache_write_input_tokens_global_min_last IS NULL) = 0
             THEN sum(uncached_input_tokens_global_min_last + output_tokens_global_min_last + cache_read_input_tokens_global_min_last + cache_write_input_tokens_global_min_last)
        END AS total_tokens,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_lower IS NULL OR output_tokens_lower IS NULL
                                        OR cache_read_input_tokens_lower IS NULL OR cache_write_input_tokens_lower IS NULL) = 0
             THEN sum(uncached_input_tokens_lower + output_tokens_lower + cache_read_input_tokens_lower + cache_write_input_tokens_lower)
        END AS total_tokens_lower_bound,
        CASE WHEN count(*) FILTER (WHERE uncached_input_tokens_upper IS NULL OR output_tokens_upper IS NULL
                                        OR cache_read_input_tokens_upper IS NULL OR cache_write_input_tokens_upper IS NULL) = 0
             THEN sum(uncached_input_tokens_upper + output_tokens_upper + cache_read_input_tokens_upper + cache_write_input_tokens_upper)
        END AS total_tokens_upper_bound
    FROM bucketed
    GROUP BY bucket_start_utc, provider, exact_model, service_tier, speed
)
SELECT json_build_object(
    'record_type', 'usage',
    'granularity', 'week',
    'bucket_start_utc', to_char(bucket_start_utc, 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'provider', provider,
    'model', exact_model,
    'source', 'claude_code_web',
    'payment_lane', 'unknown',
    'requests', requests,
    'session_copy_records', session_copy_records,
    'session_copy_excess', session_copy_excess,
    'requests_with_cross_session_copies', requests_with_cross_session_copies,
    'requests_whose_copies_cross_utc_week', requests_whose_copies_cross_utc_week,
    'max_copy_time_span_seconds', max_copy_time_span_seconds,
    'requests_with_counter_variants', requests_with_counter_variants,
    'requests_with_output_decrease', requests_with_output_decrease,
    'requests_with_missing_stop_reason_copy', requests_with_missing_stop_reason_copy,
    'api_error_messages', api_error_messages,
    'aborted_messages', aborted_messages,
    'requests_missing_message_id', requests_missing_message_id,
    'requests_with_subagent_role', requests_with_subagent_role,
    'guardian_requests', guardian_requests,
    'reported_service_tier', service_tier,
    'reported_speed', speed,
    'uncached_input_tokens', uncached_input_tokens,
    'uncached_input_tokens_lower_bound', uncached_input_tokens_lower_bound,
    'uncached_input_tokens_upper_bound', uncached_input_tokens_upper_bound,
    'cache_read_input_tokens', cache_read_input_tokens,
    'cache_read_input_tokens_lower_bound', cache_read_input_tokens_lower_bound,
    'cache_read_input_tokens_upper_bound', cache_read_input_tokens_upper_bound,
    'cache_write_input_tokens', cache_write_input_tokens,
    'cache_write_input_tokens_lower_bound', cache_write_input_tokens_lower_bound,
    'cache_write_input_tokens_upper_bound', cache_write_input_tokens_upper_bound,
    'output_tokens', output_tokens,
    'output_tokens_lower_bound', output_tokens_lower_bound,
    'output_tokens_upper_bound', output_tokens_upper_bound,
    'reasoning_output_tokens', reasoning_output_tokens,
    'reasoning_output_tokens_reported_sum', reasoning_output_tokens_reported_sum,
    'requests_with_reasoning_detail', requests_with_reasoning_detail,
    'output_tokens_estimate_basis', 'minimum last-sequence counter across session copies for the global message ID; output lower/upper bounds are min/max over every streamed event and copy',
    'total_tokens', total_tokens,
    'total_tokens_lower_bound', total_tokens_lower_bound,
    'total_tokens_upper_bound', total_tokens_upper_bound,
    'total_tokens_definition', 'derived sum of uncached input + cache read + cache write + output; not a provider-reported total or billable amount'
)::text
FROM weekly
ORDER BY bucket_start_utc, provider, exact_model NULLS FIRST, service_tier, speed;

COMMIT;
