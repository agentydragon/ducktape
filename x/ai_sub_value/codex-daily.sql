-- Never SUM repeated observations of the same day.
SELECT provider, start_date, argMax(tokens, observed_at) AS latest_tokens,
       min(observed_at) AS first_observation,
       max(observed_at) AS last_observation,
       min(tokens) AS min_reported, max(tokens) AS max_reported,
       count() AS observations
FROM aiquota.token_activity_daily
WHERE observed_at <= now64(3)
GROUP BY provider, start_date
ORDER BY provider, start_date
FORMAT JSONEachRow
