-- Langfuse v4 events_full is a ReplacingMergeTree: FINAL removes row versions.
-- Explicit upper bound excludes malformed year-9999 records.
-- The current OTel path emits GENERATION for HTTP and bookkeeping spans too.
-- 'chat ...' spans carry model calls. The older path used 'litellm_request'.
SELECT provided_model_name AS model, count() AS call_spans,
       uniqExact(trace_id) AS traces,
       countIf(empty(usage_details)) AS missing_usage,
       countIf(level = 'ERROR') AS errors,
       sumMap(usage_details) AS usage,
       sumMap(provided_usage_details) AS provided_usage,
       sum(cost_details['total']) AS recorded_cost,
       min(start_time) AS first, max(start_time) AS last
FROM langfuse.events_full FINAL
PREWHERE start_time >= now() - INTERVAL 30 DAY AND start_time <= now()
WHERE type = 'GENERATION' AND is_deleted = 0
  AND (startsWith(name, 'chat ') OR name = 'litellm_request')
GROUP BY model
ORDER BY call_spans DESC
FORMAT JSONEachRow
