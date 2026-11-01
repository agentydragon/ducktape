-- Exact gauge observations; keep the full extract in the task scratch directory.
-- Use one replica: the installation has replicated data, not independent shards.
SELECT event_id, observed_at, provider, window_name, window_seconds,
       used_percent, reset_at, extra_spend_enabled, extra_spend_used_usd
FROM aiquota.aiquota_windows
WHERE observed_at <= now64(3)
ORDER BY provider, window_seconds, window_name, observed_at
FORMAT JSONEachRow
