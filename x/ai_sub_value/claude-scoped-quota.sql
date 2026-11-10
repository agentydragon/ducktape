-- Model-scoped limits are present in raw responses beyond the typed window table.
WITH base64Decode(raw_body_base64) AS raw,
     JSONExtractArrayRaw(raw, 'weekly_scoped_shares') AS shares,
     arrayFirst(s -> JSONExtractInt(s, 'limit_index') = item_index - 1, shares) AS share
SELECT event_id, observed_at, 'claude' AS provider,
       JSONExtractString(item, 'kind') AS limit_kind,
       JSONExtractString(item, 'scope', 'model', 'display_name') AS model_scope,
       JSONExtractFloat(item, 'percent') AS used_percent,
       JSONExtractString(item, 'resets_at') AS reset_at,
       JSONExtractBool(item, 'is_active') AS is_active,
       JSONExtractRaw(share, 'allowance_percent_of_weekly') AS allowance_percent_of_weekly,
       JSONExtractRaw(share, 'used_percent_of_weekly') AS used_percent_of_weekly
FROM aiquota.raw_http_observations
ARRAY JOIN JSONExtractArrayRaw(raw, 'limits') AS item,
           arrayEnumerate(JSONExtractArrayRaw(raw, 'limits')) AS item_index
WHERE source = 'claude' AND observed_at <= now64(3)
  AND JSONExtractString(item, 'kind') = 'weekly_scoped'
ORDER BY observed_at, model_scope
FORMAT JSONEachRow
