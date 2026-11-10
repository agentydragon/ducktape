SELECT source, JSONExtractString(normalized_body, 'payload', 'kind') AS kind,
       count() AS n, min(observed_at) AS first, max(observed_at) AS last,
       countIf(error != '') AS failures,
       countIf(raw_body_truncated) AS truncated
FROM aiquota.raw_http_observations
GROUP BY source, kind
ORDER BY source, kind
FORMAT JSONEachRow
