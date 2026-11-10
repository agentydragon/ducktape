-- Replace DATE_START_UTC and DATE_END_UTC with an inclusive start and exclusive
-- end UTC timestamp (YYYY-MM-DD HH:MM:SS). `FINAL` deduplicates Langfuse row revisions;
-- separate aggregate-only SQL audits provider response/call ID reuse before
-- these span counts are treated as requests. No message/tool content or raw
-- identifiers are returned.
WITH calls_base AS (
    SELECT project_id, trace_id, span_id, start_time, level,
           source AS ingestion_source, name AS observation_name,
           coalesce(nullIf(provided_model_name, ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.request.model')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.model_name')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.response.model')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.response.model')), ''),
                    '') AS model,
           coalesce(
               nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.litellm.metadata.user_api_key_alias')), ''),
               nullIf(JSONExtractString(JSONExtractString(arrayElement(metadata_values, indexOf(metadata_names, 'attributes')), 'metadata'), 'user_api_key_alias'), ''),
               nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.litellm.key_alias')), ''),
               '(unattributed)'
           ) AS virtual_key_alias,
           coalesce(nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.litellm.request.route')), ''), '') AS route_observed,
           coalesce(
               nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.provider.name')), ''),
               nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.provider')), ''),
               nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'provider')), ''),
               ''
           ) AS provider_observed,
           coalesce(nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.request.model')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.model_name')), ''),
                    '') AS request_model_observed,
           coalesce(nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.response.model')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.response.model')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.litellm.provider.model')), ''),
                    '') AS response_model_observed,
           coalesce(nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.litellm.call_id')), ''), '') AS call_id,
           coalesce(nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.response.id')), ''),
                    nullIf(arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.response.id')), ''),
                    '') AS response_id,
           usage_details, provided_usage_details,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.usage.input_tokens')) AS gen_ai_input_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.usage.output_tokens')) AS gen_ai_output_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.usage.total_tokens')) AS gen_ai_total_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.usage.cache_read.input_tokens')) AS gen_ai_cache_read_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.gen_ai.usage.cache_creation.input_tokens')) AS gen_ai_cache_write_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.token_count.prompt')) AS llm_prompt_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.token_count.completion')) AS llm_completion_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.token_count.completion_details.reasoning')) AS llm_reasoning_attr,
           arrayElement(metadata_values, indexOf(metadata_names, 'attributes.llm.token_count.prompt_details.cache_read')) AS llm_cache_read_attr
    FROM langfuse.events_full FINAL
    PREWHERE is_deleted = 0 AND type = 'GENERATION'
      AND start_time >= toDateTime64('DATE_START_UTC', 6, 'UTC')
      AND start_time < toDateTime64('DATE_END_UTC', 6, 'UTC')
      AND (name = 'litellm_request'
       OR startsWith(name, 'chat ')
       OR startsWith(name, 'embeddings ')
       OR name IN ('raw_gen_ai_request', 'cluster-litellm-langfuse-smoke'))
), calls AS (
    SELECT *,
           multiIf(notEmpty(response_id), 'provider_response_id',
                   notEmpty(call_id), 'litellm_call_id', 'span_id') AS request_identity_kind,
           multiIf(notEmpty(response_id), concat('response:', response_id),
                   notEmpty(call_id), concat('call:', call_id),
                   notEmpty(span_id), concat('span:', span_id),
                   concat('span-fallback:', trace_id, ':', toString(start_time), ':', observation_name)) AS request_identity_value
    FROM calls_base
), per_request AS (
    SELECT project_id, request_identity_kind, request_identity_value,
           count() AS span_events,
           argMax(tuple(start_time, level, trace_id, ingestion_source, observation_name, model,
                        virtual_key_alias, route_observed, provider_observed,
                        request_model_observed, response_model_observed,
                        call_id, response_id, usage_details, provided_usage_details,
                        gen_ai_input_attr, gen_ai_output_attr, gen_ai_total_attr,
                        gen_ai_cache_read_attr, gen_ai_cache_write_attr,
                        llm_prompt_attr, llm_completion_attr, llm_reasoning_attr,
                        llm_cache_read_attr), tuple(start_time, span_id)) AS picked
    FROM calls
    GROUP BY project_id, request_identity_kind, request_identity_value
), calls_dedup AS (
    SELECT project_id, request_identity_kind, span_events,
           tupleElement(picked, 1) AS start_time,
           tupleElement(picked, 2) AS level,
           tupleElement(picked, 3) AS trace_id,
           tupleElement(picked, 4) AS ingestion_source,
           tupleElement(picked, 5) AS observation_name,
           tupleElement(picked, 6) AS model,
           tupleElement(picked, 7) AS virtual_key_alias,
           tupleElement(picked, 8) AS route_observed,
           tupleElement(picked, 9) AS provider_observed,
           tupleElement(picked, 10) AS request_model_observed,
           tupleElement(picked, 11) AS response_model_observed,
           tupleElement(picked, 12) AS call_id,
           tupleElement(picked, 13) AS response_id,
           tupleElement(picked, 14) AS usage_details,
           tupleElement(picked, 15) AS provided_usage_details,
           tupleElement(picked, 16) AS gen_ai_input_attr,
           tupleElement(picked, 17) AS gen_ai_output_attr,
           tupleElement(picked, 18) AS gen_ai_total_attr,
           tupleElement(picked, 19) AS gen_ai_cache_read_attr,
           tupleElement(picked, 20) AS gen_ai_cache_write_attr,
           tupleElement(picked, 21) AS llm_prompt_attr,
           tupleElement(picked, 22) AS llm_completion_attr,
           tupleElement(picked, 23) AS llm_reasoning_attr,
           tupleElement(picked, 24) AS llm_cache_read_attr
    FROM per_request
), calls_classified AS (
    SELECT *,
           if(mapContains(usage_details, 'input'), usage_details['input'], toUInt64OrZero(gen_ai_input_attr)) AS input_value,
           if(mapContains(usage_details, 'output'), usage_details['output'], toUInt64OrZero(gen_ai_output_attr)) AS output_value,
           if(mapContains(usage_details, 'total'), usage_details['total'], toUInt64OrZero(gen_ai_total_attr)) AS total_value,
           if(mapContains(usage_details, 'input_cached_tokens'), usage_details['input_cached_tokens'],
              toUInt64OrZero(if(notEmpty(gen_ai_cache_read_attr), gen_ai_cache_read_attr, llm_cache_read_attr))) AS cache_read_value,
           if(mapContains(usage_details, 'input_cache_creation'), usage_details['input_cache_creation'],
              toUInt64OrZero(gen_ai_cache_write_attr)) AS cache_write_value,
           greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens'],
                    toUInt64OrZero(llm_reasoning_attr)) AS reasoning_value
    FROM calls_dedup
)
SELECT formatDateTime(toStartOfInterval(toTimeZone(start_time, 'UTC'), INTERVAL 5 MINUTE, 'UTC'), '%Y-%m-%dT%H:%i:%SZ', 'UTC') AS bucket_start_utc,
       'langfuse' AS source,
       ingestion_source,
       multiIf(observation_name = 'litellm_request', 'legacy_litellm_request',
               startsWith(observation_name, 'chat '), 'otel_chat_model_call',
               startsWith(observation_name, 'embeddings '), 'otel_embeddings_model_call',
               observation_name = 'raw_gen_ai_request', 'candidate_raw_gen_ai_request',
               'synthetic_smoke') AS schema_epoch,
       multiIf(
           startsWith(model, 'ollama/') OR startsWith(request_model_observed, 'ollama/')
               OR startsWith(model, 'antigravity/') OR startsWith(request_model_observed, 'antigravity/'), 'unknown',
           startsWith(model, 'openai/') OR startsWith(model, 'chatgpt/') OR startsWith(model, 'chatgpt-sub/')
               OR startsWith(request_model_observed, 'openai/') OR startsWith(request_model_observed, 'chatgpt/') OR startsWith(request_model_observed, 'chatgpt-sub/'), 'openai',
           startsWith(model, 'anthropic/') OR startsWith(model, 'anthropic-api/') OR startsWith(model, 'anthropic-max20/')
               OR startsWith(request_model_observed, 'anthropic/') OR startsWith(request_model_observed, 'anthropic-api/') OR startsWith(request_model_observed, 'anthropic-max20/'), 'anthropic',
           'unknown'
       ) AS provider,
       nullIf(model, '') AS model,
       virtual_key_alias,
       multiIf(startsWith(model, 'chatgpt-sub/') OR startsWith(model, 'chatgpt/')
                   OR startsWith(request_model_observed, 'chatgpt-sub/') OR startsWith(request_model_observed, 'chatgpt/'),
               'openai_subscription_via_cli_proxy_oauth',
               startsWith(model, 'anthropic-max20/') OR startsWith(request_model_observed, 'anthropic-max20/'),
               'anthropic_subscription_via_cli_proxy_oauth',
               startsWith(model, 'anthropic-api/') OR startsWith(request_model_observed, 'anthropic-api/'),
               'anthropic_direct_api_key',
               'unknown_route_scope') AS payment_lane,
       multiIf(startsWith(model, 'chatgpt-sub/') OR startsWith(model, 'chatgpt/')
                   OR startsWith(request_model_observed, 'chatgpt-sub/') OR startsWith(request_model_observed, 'chatgpt/'),
               'cli_proxy_codex_oauth_session',
               startsWith(model, 'anthropic-max20/') OR startsWith(request_model_observed, 'anthropic-max20/'),
               'cli_proxy_claude_oauth_session',
               startsWith(model, 'anthropic-api/') OR startsWith(request_model_observed, 'anthropic-api/'),
               'direct_anthropic_api_key_account',
               'unknown') AS account_scope,
       route_observed,
       provider_observed,
       count() AS span_requests_after_final,
       sum(span_events) AS span_events_before_id_dedup,
       countIf(level = 'ERROR') AS error_requests,
       countIf(empty(model)) AS unresolved_model_requests,
       countIf(empty(usage_details)) AS missing_usage_requests,
       countIf(virtual_key_alias = '(unattributed)') AS missing_alias_requests,
       countIf(notEmpty(call_id)) AS with_litellm_call_id_requests,
       countIf(notEmpty(call_id)) AS distinct_litellm_call_ids,
       countIf(request_identity_kind = 'litellm_call_id') AS distinct_litellm_call_ids_without_response_id,
       countIf(request_identity_kind = 'provider_response_id') AS with_provider_response_id_requests,
       countIf(request_identity_kind = 'provider_response_id') AS distinct_provider_response_ids,
       countIf(request_identity_kind = 'span_id') AS no_explicit_id_requests,
       uniqExact(trace_id) AS traces,
       countIf(mapContains(usage_details, 'input')) AS input_known_requests,
       countIf(mapContains(usage_details, 'input_cached_tokens') OR notEmpty(gen_ai_cache_read_attr) OR notEmpty(llm_cache_read_attr)) AS cache_read_known_requests,
       countIf(mapContains(usage_details, 'input_cache_creation') OR notEmpty(gen_ai_cache_write_attr)) AS cache_write_known_requests,
       countIf(mapContains(usage_details, 'output')) AS output_known_requests,
       countIf(mapContains(usage_details, 'completion_details.reasoning') OR mapContains(usage_details, 'output_reasoning_tokens') OR notEmpty(llm_reasoning_attr)) AS reasoning_known_requests,
       countIf(mapContains(usage_details, 'total') OR notEmpty(gen_ai_total_attr)) AS total_known_requests,
       countIf((mapContains(usage_details, 'input') OR notEmpty(gen_ai_input_attr)) AND cache_read_value + cache_write_value > 0) AS input_positive_cache_requests,
       countIf((mapContains(usage_details, 'input') OR notEmpty(gen_ai_input_attr)) AND cache_read_value + cache_write_value > 0
               AND (mapContains(usage_details, 'total') OR notEmpty(gen_ai_total_attr))
               AND (total_value = input_value + output_value
                    OR total_value = input_value + output_value + reasoning_value)) AS input_total_inclusive_requests,
       countIf((mapContains(usage_details, 'input') OR notEmpty(gen_ai_input_attr)) AND cache_read_value + cache_write_value > 0
               AND (mapContains(usage_details, 'total') OR notEmpty(gen_ai_total_attr))
               AND (total_value = input_value + cache_read_value + cache_write_value + output_value
                    OR total_value = input_value + cache_read_value + cache_write_value + output_value + reasoning_value)) AS input_total_exclusive_requests,
       countIf((mapContains(usage_details, 'input') OR notEmpty(gen_ai_input_attr)) AND cache_read_value + cache_write_value > 0
               AND (NOT (mapContains(usage_details, 'total') OR notEmpty(gen_ai_total_attr))
                    OR (total_value != input_value + output_value
                        AND total_value != input_value + output_value + reasoning_value
                        AND total_value != input_value + cache_read_value + cache_write_value + output_value
                        AND total_value != input_value + cache_read_value + cache_write_value + output_value + reasoning_value))) AS input_total_algebra_unknown_requests,
       countIf((mapContains(usage_details, 'input') OR notEmpty(gen_ai_input_attr))
               AND NOT mapContains(usage_details, 'input_cached_tokens') AND empty(gen_ai_cache_read_attr) AND empty(llm_cache_read_attr)
               AND NOT mapContains(usage_details, 'input_cache_creation') AND empty(gen_ai_cache_write_attr)) AS input_cache_unreported_requests,
       countIf(mapContains(usage_details, 'completion_details.reasoning') AND mapContains(usage_details, 'output_reasoning_tokens')) AS both_reasoning_keys_requests,
       countIf(mapContains(usage_details, 'completion_details.reasoning') AND mapContains(usage_details, 'output_reasoning_tokens')
               AND usage_details['completion_details.reasoning'] != usage_details['output_reasoning_tokens']) AS conflicting_reasoning_keys_requests,
       countIf(greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens']) > 0) AS reasoning_positive_requests,
       countIf(mapContains(usage_details, 'total')
               AND mapContains(usage_details, 'input') AND mapContains(usage_details, 'output')
               AND greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens']) > 0
               AND (usage_details['total'] = usage_details['input'] + usage_details['output'] + greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens'])
                    OR usage_details['total'] = usage_details['input'] + usage_details['input_cached_tokens'] + usage_details['input_cache_creation'] + usage_details['output'] + greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens']))) AS reasoning_total_additive_requests,
       countIf(mapContains(usage_details, 'total')
               AND mapContains(usage_details, 'input') AND mapContains(usage_details, 'output')
               AND greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens']) > 0
               AND (usage_details['total'] = usage_details['input'] + usage_details['output']
                    OR usage_details['total'] = usage_details['input'] + usage_details['input_cached_tokens'] + usage_details['input_cache_creation'] + usage_details['output'])) AS reasoning_total_subset_requests,
       countIf(greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens']) > 0
               AND (NOT mapContains(usage_details, 'total') OR NOT mapContains(usage_details, 'input') OR NOT mapContains(usage_details, 'output')
                    OR (usage_details['total'] != usage_details['input'] + usage_details['output']
                        AND usage_details['total'] != usage_details['input'] + usage_details['input_cached_tokens'] + usage_details['input_cache_creation'] + usage_details['output']
                        AND usage_details['total'] != usage_details['input'] + usage_details['output'] + greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens'])
                        AND usage_details['total'] != usage_details['input'] + usage_details['input_cached_tokens'] + usage_details['input_cache_creation'] + usage_details['output'] + greatest(usage_details['completion_details.reasoning'], usage_details['output_reasoning_tokens'])))) AS reasoning_total_algebra_unknown_requests,
       countIf(usage_details = provided_usage_details) AS usage_maps_equal_requests,
       sumMap(usage_details) AS raw_usage_sums,
       sumMap(provided_usage_details) AS raw_provided_usage_sums,
       sum(toUInt64OrZero(gen_ai_input_attr)) AS gen_ai_input_attr_sum,
       sum(toUInt64OrZero(gen_ai_output_attr)) AS gen_ai_output_attr_sum,
       sum(toUInt64OrZero(gen_ai_total_attr)) AS gen_ai_total_attr_sum,
       sum(toUInt64OrZero(gen_ai_cache_read_attr)) AS gen_ai_cache_read_attr_sum,
       sum(toUInt64OrZero(gen_ai_cache_write_attr)) AS gen_ai_cache_write_attr_sum,
       sum(toUInt64OrZero(llm_prompt_attr)) AS llm_prompt_attr_sum,
       sum(toUInt64OrZero(llm_completion_attr)) AS llm_completion_attr_sum,
       sum(toUInt64OrZero(llm_reasoning_attr)) AS llm_reasoning_attr_sum,
       sum(toUInt64OrZero(llm_cache_read_attr)) AS llm_cache_read_attr_sum
FROM calls_classified
GROUP BY bucket_start_utc, source, ingestion_source, schema_epoch, provider, model,
         virtual_key_alias, payment_lane, account_scope, route_observed, provider_observed
ORDER BY bucket_start_utc, provider, model, payment_lane, virtual_key_alias
SETTINGS max_threads = 1,
         max_bytes_before_external_group_by = 67108864,
         max_bytes_before_external_sort = 67108864
FORMAT JSONEachRow;
