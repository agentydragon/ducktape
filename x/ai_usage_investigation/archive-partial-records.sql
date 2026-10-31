-- Count physical records in the latest incomplete version of each session.
-- Inherited prefixes are reconstructed once; observations use EXISTS so multiple counters
-- on one payload never multiply physical-record or usage-bearing-record counts.
-- A NULL parse_error means the raw JSON parsed successfully, including NUL-bearing values
-- that JSONB could not store. Only persisted parser errors are classified as unreadable.
WITH RECURSIVE latest AS (
    SELECT DISTINCT ON (session_id) *
    FROM file_versions
    ORDER BY session_id, last_seen_at DESC, first_seen_at DESC, id DESC
), chain AS (
    SELECT s.machine_id, s.harness, l.id, l.parent_version_id,
           l.shared_prefix_record_count, l.record_count AS upper_ordinal
    FROM latest l
    JOIN sessions s ON s.id = l.session_id
    WHERE NOT l.complete
    UNION ALL
    SELECT c.machine_id, c.harness, p.id, p.parent_version_id,
           p.shared_prefix_record_count,
           least(c.upper_ordinal, c.shared_prefix_record_count)
    FROM chain c
    JOIN file_versions p ON p.id = c.parent_version_id
), records AS (
    SELECT c.machine_id, c.harness, r.ordinal, p.id AS payload_id, p.kind,
           p.parse_error IS NULL AS readable,
           CASE
               WHEN p.parse_error IS NULL THEN 'readable'
               ELSE coalesce(nullif(split_part(p.parse_error, ':', 1), ''), 'parse_error')
           END AS issue,
           EXISTS (
               SELECT 1 FROM usage_observations o WHERE o.payload_id = p.id
           ) AS has_usage
    FROM chain c
    CROSS JOIN LATERAL (
        SELECT ordinal, payload_id FROM file_version_records
        WHERE file_version_id = c.id AND ordinal < c.upper_ordinal
        OFFSET 0
    ) r
    CROSS JOIN LATERAL (
        SELECT id, kind, parse_error FROM record_payloads
        WHERE id = r.payload_id OFFSET 0
    ) p
)
SELECT json_build_object(
    'observed_at', now(),
    'machine', machine_id,
    'harness', harness,
    'issue', issue,
    'physical_records', count(*),
    'readable_records', count(*) FILTER (WHERE readable),
    'parse_error_records', count(*) FILTER (WHERE NOT readable),
    'usage_bearing_records', count(*) FILTER (WHERE has_usage),
    'readable_usage_bearing_records', count(*) FILTER (WHERE readable AND has_usage)
) AS report
FROM records
GROUP BY machine_id, harness, issue
ORDER BY machine_id, harness, issue;
