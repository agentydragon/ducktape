-- Pseudonymize credit IDs; retain status transitions, not just latest state.
SELECT substring(hex(SHA256(credit_id)), 1, 12) AS credit,
       reset_type, status, min(granted_at) AS granted_at,
       min(observed_at) AS first_seen, max(observed_at) AS last_seen,
       min(expires_at) AS expires_at, count() AS observations
FROM aiquota.reset_credits
WHERE observed_at <= now64(3)
GROUP BY credit_id, reset_type, status
ORDER BY granted_at, credit, first_seen
FORMAT JSONEachRow
