-- Exact gauge observations; keep the full extract in the task scratch directory.
-- Use one replica: the installation has replicated data, not independent shards.
WITH codex_plan AS (
    SELECT event_id,
           JSONExtractString(base64Decode(raw_body_base64), 'plan_type') AS reported_plan_type
    FROM aiquota.raw_http_observations
    WHERE source = 'codex' AND length(quota_windows) > 0
)
SELECT q.event_id, q.observed_at, q.provider, q.window_name, q.window_seconds,
       q.used_percent, q.reset_at, q.extra_spend_enabled, q.extra_spend_used_usd,
       if(q.provider = 'codex', ifNull(plan.reported_plan_type, ''), '') AS reported_plan_type
FROM aiquota.aiquota_windows AS q
LEFT JOIN codex_plan AS plan USING (event_id)
WHERE q.observed_at <= now64(3)
ORDER BY q.provider, q.window_seconds, q.window_name, q.observed_at
FORMAT JSONEachRow
