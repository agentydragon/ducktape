-- v4 flattened the metadata names. Old rows retain a nested attributes JSON.
WITH mapFromArrays(metadata_names, metadata_values) AS metadata,
     coalesce(nullIf(metadata['attributes.litellm.metadata.user_api_key_alias'], ''),
              nullIf(JSONExtractString(JSONExtractString(metadata['attributes'],
                     'metadata'), 'user_api_key_alias'), ''), '(unattributed)') AS alias
SELECT alias, provided_model_name AS model, count() AS call_spans,
       countIf(empty(usage_details)) AS missing_usage,
       sumMap(usage_details) AS usage
FROM langfuse.events_full FINAL
PREWHERE start_time >= now() - INTERVAL 7 DAY AND start_time <= now()
WHERE type = 'GENERATION' AND is_deleted = 0
  AND (startsWith(name, 'chat ') OR name = 'litellm_request')
GROUP BY alias, model
ORDER BY call_spans DESC
FORMAT JSONEachRow
