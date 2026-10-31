-- Normalized extra_spend disappears when Claude disables overage, even though
-- the raw response retains spend.used. Recover the ledger independently of enabled.
WITH base64Decode(raw_body_base64) AS raw
SELECT observed_at, source,
       JSONExtractRaw(raw, 'spend', 'used') AS claude_spend_used,
       JSONExtractRaw(raw, 'extra_usage') AS claude_legacy_extra,
       JSONExtractRaw(normalized_body, 'last_output', 'result', 'paid_credits') AS codex_paid,
       JSONExtractRaw(normalized_body, 'last_output', 'result', 'available_reset_credits') AS banked_resets,
       JSONExtractRaw(raw, 'seven_day_breakdown') AS surface_breakdown
FROM aiquota.raw_http_observations
WHERE length(quota_windows) > 0 AND observed_at <= now64(3)
ORDER BY source, observed_at
FORMAT JSONEachRow
