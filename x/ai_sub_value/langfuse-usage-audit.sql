-- Aggregate-only schema/retention audit. Never select input/output payloads.
SELECT type, name, source, service_name,
       count() AS rows,
       uniqExact(span_id) AS span_ids,
       uniqExact(trace_id) AS traces,
       countIf(empty(provided_model_name)) AS missing_model,
       countIf(empty(usage_details)) AS missing_usage,
       countIf(start_time >= toDateTime64('9999-01-01 00:00:00', 6)) AS year_9999,
       min(start_time) AS first_time,
       max(start_time) AS last_time
FROM langfuse.events_full FINAL
PREWHERE is_deleted = 0
GROUP BY type, name, source, service_name
ORDER BY rows DESC
FORMAT JSONEachRow;

-- Only key names/counts, not metadata values.
SELECT key, count() AS rows
FROM langfuse.events_full FINAL
ARRAY JOIN metadata_names AS key
PREWHERE is_deleted = 0
GROUP BY key
ORDER BY rows DESC
FORMAT JSONEachRow;

-- Usage key vocabulary and occurrence counts by event/model-call name.
SELECT type, name, key, count() AS rows,
       sum(mapContains(usage_details, key)) AS rows_with_usage_key
FROM langfuse.events_full FINAL
ARRAY JOIN arrayDistinct(arrayConcat(mapKeys(usage_details), mapKeys(provided_usage_details))) AS key
PREWHERE is_deleted = 0
GROUP BY type, name, key
ORDER BY rows DESC
FORMAT JSONEachRow;
