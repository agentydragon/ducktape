-- Aggregate-only audit of LiteLLM call-ID/provider response-ID collisions.
-- Replace DATE_START_UTC and DATE_END_UTC with UTC timestamps. Identifiers are
-- grouped and discarded inside ClickHouse; this query never returns an ID.
WITH calls AS (
    SELECT project_id, trace_id, span_id, name, provided_model_name, usage_details,
           coalesce(nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.litellm.call_id')), ''), '') AS litellm_call_id,
           coalesce(nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.response.id')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.response.id')), ''),
                    '') AS provider_response_id,
           multiIf(name = 'litellm_request', 'legacy_litellm_request',
                   startsWith(name, 'chat '), 'otel_chat_model_call',
                   startsWith(name, 'embeddings '), 'otel_embeddings_model_call',
                   name = 'raw_gen_ai_request', 'candidate_raw_gen_ai_request',
                   name = 'cluster-litellm-langfuse-smoke', 'synthetic_smoke',
                   'other_generation') AS call_adapter
    FROM langfuse.events_full FINAL
    PREWHERE is_deleted = 0 AND type = 'GENERATION'
      AND start_time >= toDateTime64('DATE_START_UTC', 6, 'UTC')
      AND start_time < toDateTime64('DATE_END_UTC', 6, 'UTC')
    WHERE name = 'litellm_request'
       OR startsWith(name, 'chat ')
       OR startsWith(name, 'embeddings ')
       OR name IN ('raw_gen_ai_request', 'cluster-litellm-langfuse-smoke')
), identifiers AS (
    SELECT project_id, trace_id, span_id, call_adapter, provided_model_name, usage_details, provider_response_id,
           identifier.1 AS identifier_kind, identifier.2 AS identifier_value
    FROM calls
    ARRAY JOIN [('litellm_call_id', litellm_call_id), ('provider_response_id', provider_response_id)] AS identifier
    WHERE notEmpty(identifier.2)
), id_groups AS (
    SELECT identifier_kind, project_id, identifier_value,
           count() AS event_rows_per_id,
           countIf(empty(provider_response_id)) AS events_without_response_id_per_id,
           uniqExact(tuple(trace_id, span_id)) AS spans_per_id,
           uniqExact(call_adapter) AS adapters_per_id,
           uniqExact(provided_model_name) AS models_per_id,
           uniqExact(usage_details) AS usage_variants_per_id,
           sum(usage_details['input']) AS input_all_rows,
           max(usage_details['input']) AS input_max_one_row,
           sum(usage_details['input_cached_tokens']) AS cache_read_all_rows,
           max(usage_details['input_cached_tokens']) AS cache_read_max_one_row,
           sum(usage_details['input_cache_creation']) AS cache_write_all_rows,
           max(usage_details['input_cache_creation']) AS cache_write_max_one_row,
           sum(usage_details['output']) AS output_all_rows,
           max(usage_details['output']) AS output_max_one_row,
           sum(greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens'])) AS reasoning_all_rows,
           max(greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens'])) AS reasoning_max_one_row,
           sum(usage_details['total']) AS total_all_rows,
           max(usage_details['total']) AS total_max_one_row
    FROM identifiers
    GROUP BY identifier_kind, project_id, identifier_value
)
SELECT identifier_kind,
       count() AS distinct_identifiers,
       sum(event_rows_per_id) AS events_with_identifier,
       countIf(event_rows_per_id > 1) AS colliding_identifier_groups,
       sum(event_rows_per_id - 1) AS duplicate_candidate_event_rows,
       sumIf(greatest(events_without_response_id_per_id - 1, 0), identifier_kind = 'litellm_call_id') AS duplicate_call_id_rows_without_response_id,
       countIf(event_rows_per_id > 1 AND (adapters_per_id > 1 OR models_per_id > 1 OR usage_variants_per_id > 1)) AS nonidentical_collision_groups,
       countIf(adapters_per_id > 1) AS cross_adapter_identifier_groups,
       sumIf(input_all_rows - input_max_one_row, event_rows_per_id > 1) AS duplicate_input_token_sensitivity,
       sumIf(cache_read_all_rows - cache_read_max_one_row, event_rows_per_id > 1) AS duplicate_cache_read_token_sensitivity,
       sumIf(cache_write_all_rows - cache_write_max_one_row, event_rows_per_id > 1) AS duplicate_cache_write_token_sensitivity,
       sumIf(output_all_rows - output_max_one_row, event_rows_per_id > 1) AS duplicate_output_token_sensitivity,
       sumIf(reasoning_all_rows - reasoning_max_one_row, event_rows_per_id > 1) AS duplicate_reasoning_token_sensitivity,
       sumIf(total_all_rows - total_max_one_row, event_rows_per_id > 1) AS duplicate_total_token_sensitivity
FROM id_groups
GROUP BY identifier_kind
ORDER BY identifier_kind
FORMAT JSONEachRow;
