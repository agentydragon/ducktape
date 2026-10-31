-- Cross-session message ID collision audit. Returns aggregate statistics only.
-- No IDs, prompts, result text or tool content leave PostgreSQL.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '120s';

WITH source AS MATERIALIZED (
    SELECT e.session_id, e.sequence_num, e.created_at,
           e.payload->'message'->>'id' AS message_id,
           e.payload->'message'->>'model' AS model,
           CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'input_tokens') = 'number'
                THEN (e.payload->'message'->'usage'->>'input_tokens')::bigint END AS input_tokens,
           CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'output_tokens') = 'number'
                THEN (e.payload->'message'->'usage'->>'output_tokens')::bigint END AS output_tokens,
           CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'cache_read_input_tokens') = 'number'
                THEN (e.payload->'message'->'usage'->>'cache_read_input_tokens')::bigint END AS cache_read_tokens,
           CASE WHEN jsonb_typeof(e.payload->'message'->'usage'->'cache_creation_input_tokens') = 'number'
                THEN (e.payload->'message'->'usage'->>'cache_creation_input_tokens')::bigint END AS cache_write_tokens,
           CASE WHEN jsonb_typeof(e.payload->'timestamp') = 'string'
                     AND e.payload->>'timestamp' ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T'
                THEN (e.payload->>'timestamp')::timestamptz END AS payload_timestamp,
           e.payload->'message'->>'stop_reason' AS stop_reason
    FROM events e
    WHERE e.event_type = 'assistant'
      AND NULLIF(e.payload->'message'->>'id', '') IS NOT NULL
), per_session_message AS MATERIALIZED (
    SELECT DISTINCT ON (session_id, message_id)
           session_id, message_id, sequence_num, created_at, payload_timestamp, model, input_tokens,
           output_tokens, cache_read_tokens, cache_write_tokens, stop_reason
    FROM source
    ORDER BY session_id, message_id, sequence_num DESC
), reused_ids AS MATERIALIZED (
    SELECT message_id, count(*) AS session_copies,
           count(DISTINCT sequence_num) AS sequence_values,
           count(DISTINCT model) AS models,
           count(DISTINCT input_tokens) AS input_values,
           count(DISTINCT output_tokens) AS output_values,
           count(DISTINCT cache_read_tokens) AS cache_read_values,
           count(DISTINCT cache_write_tokens) AS cache_write_values,
           count(DISTINCT created_at) AS last_times,
           count(DISTINCT payload_timestamp) AS payload_time_values,
           sum(input_tokens) AS input_copy_sum,
           min(input_tokens) AS input_global_min,
           max(input_tokens) AS input_global_max,
           sum(output_tokens) AS output_copy_sum,
           min(output_tokens) AS output_global_min,
           max(output_tokens) AS output_global_max,
           sum(cache_read_tokens) AS cache_read_copy_sum,
           min(cache_read_tokens) AS cache_read_global_min,
           max(cache_read_tokens) AS cache_read_global_max,
           sum(cache_write_tokens) AS cache_write_copy_sum,
           min(cache_write_tokens) AS cache_write_global_min,
           max(cache_write_tokens) AS cache_write_global_max,
           min(created_at) AS earliest_event_at,
           max(created_at) AS latest_event_at,
           min(payload_timestamp) AS earliest_payload_time,
           max(payload_timestamp) AS latest_payload_time,
           count(DISTINCT stop_reason) AS stop_reasons
    FROM per_session_message
    GROUP BY message_id
    HAVING count(*) > 1
)
SELECT json_build_object(
    'reused_message_ids', count(*),
    'per_session_copies_for_reused_ids', sum(session_copies),
    'excess_session_copies', sum(session_copies - 1),
    'ids_with_same_sequence_num_in_every_session', count(*) FILTER (WHERE sequence_values = 1),
    'ids_with_sequence_num_conflicts', count(*) FILTER (WHERE sequence_values > 1),
    'ids_with_copies_within_1_hour', count(*) FILTER (WHERE latest_event_at - earliest_event_at <= interval '1 hour'),
    'ids_with_copies_within_1_day', count(*) FILTER (WHERE latest_event_at - earliest_event_at <= interval '1 day'),
    'max_copy_time_gap_seconds', max(extract(epoch FROM latest_event_at - earliest_event_at)),
    'ids_with_model_conflicts', count(*) FILTER (WHERE models > 1),
    'ids_with_input_counter_conflicts', count(*) FILTER (WHERE input_values > 1),
    'ids_with_output_counter_conflicts', count(*) FILTER (WHERE output_values > 1),
    'ids_with_cache_read_counter_conflicts', count(*) FILTER (WHERE cache_read_values > 1),
    'ids_with_cache_write_counter_conflicts', count(*) FILTER (WHERE cache_write_values > 1),
    'ids_with_last_timestamp_conflicts', count(*) FILTER (WHERE last_times > 1),
    'ids_with_payload_timestamp_on_all_copies', count(*) FILTER (WHERE payload_time_values = session_copies),
    'ids_with_same_payload_timestamp_on_every_copy', count(*) FILTER (WHERE payload_time_values = 1),
    'ids_with_differing_payload_timestamps', count(*) FILTER (WHERE payload_time_values > 1),
    'earliest_reused_id_event_at', min(earliest_event_at),
    'latest_reused_id_event_at', max(latest_event_at),
    'reused_ids_seen_in_last_30d', count(*) FILTER (WHERE latest_event_at >= clock_timestamp() - interval '30 days'),
    'reused_ids_crossing_utc_week', count(*) FILTER (
        WHERE date_trunc('week', earliest_event_at AT TIME ZONE 'UTC')
           <> date_trunc('week', latest_event_at AT TIME ZONE 'UTC')
    ),
    'recent_reused_id_excess_copies', sum(session_copies - 1) FILTER (WHERE latest_event_at >= clock_timestamp() - interval '30 days'),
    'reused_id_uncached_input_session_copy_sum', sum(input_copy_sum),
    'reused_id_uncached_input_global_unique_min', sum(input_global_min),
    'reused_id_uncached_input_global_unique_max', sum(input_global_max),
    'reused_id_output_session_copy_sum', sum(output_copy_sum),
    'reused_id_output_global_unique_min', sum(output_global_min),
    'reused_id_output_global_unique_max', sum(output_global_max),
    'reused_id_output_duplicate_token_excess_lower', sum(output_copy_sum - output_global_max),
    'reused_id_output_duplicate_token_excess_upper', sum(output_copy_sum - output_global_min),
    'recent_reused_id_output_copy_sum', sum(output_copy_sum) FILTER (WHERE latest_event_at >= clock_timestamp() - interval '30 days'),
    'recent_reused_id_output_global_min', sum(output_global_min) FILTER (WHERE latest_event_at >= clock_timestamp() - interval '30 days'),
    'recent_reused_id_output_global_max', sum(output_global_max) FILTER (WHERE latest_event_at >= clock_timestamp() - interval '30 days'),
    'reused_id_cache_read_session_copy_sum', sum(cache_read_copy_sum),
    'reused_id_cache_read_global_unique_min', sum(cache_read_global_min),
    'reused_id_cache_read_global_unique_max', sum(cache_read_global_max),
    'reused_id_cache_write_session_copy_sum', sum(cache_write_copy_sum),
    'reused_id_cache_write_global_unique_min', sum(cache_write_global_min),
    'reused_id_cache_write_global_unique_max', sum(cache_write_global_max),
    'ids_with_stop_reason_conflicts', count(*) FILTER (WHERE stop_reasons > 1),
    'fully_matching_reused_ids', count(*) FILTER (
       WHERE models = 1 AND input_values <= 1 AND output_values <= 1
         AND cache_read_values <= 1 AND cache_write_values <= 1
         AND last_times = 1 AND stop_reasons <= 1
    )
)::text
FROM reused_ids;

COMMIT;
