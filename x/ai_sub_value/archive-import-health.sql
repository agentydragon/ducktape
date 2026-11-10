-- Run alongside archive-coverage.sql after an import; aggregate metadata only.
SELECT json_build_object(
    'observed_at', now(), 'machine', machine_id,
    'inventory_status', inventory_status, 'processing_status', processing_status,
    'metadata_status', codex_metadata_status, 'capture_status', capture_status,
    'snapshots', count(*), 'last_capture', max(captured_at))
FROM restic_snapshots
GROUP BY machine_id, inventory_status, processing_status,
         codex_metadata_status, capture_status
ORDER BY machine_id, inventory_status, processing_status, codex_metadata_status;

SELECT json_build_object(
    'observed_at', now(), 'machine', machine_id, 'status', status,
    'files', count(*), 'with_version', count(file_version_id))
FROM snapshot_files
GROUP BY machine_id, status
ORDER BY machine_id, status;

SELECT json_build_object(
    'observed_at', now(), 'machine', machine_id,
    'harness', harness, 'root', root_path)
FROM session_roots
ORDER BY machine_id, harness, root_path;

WITH latest AS (
    SELECT DISTINCT ON (machine_id)
           machine_id, snapshot_id, captured_at, comparison_snapshot_id
    FROM restic_snapshots
    WHERE inventory_status = 'complete'
    ORDER BY machine_id, captured_at DESC, snapshot_id DESC
), files AS (
    SELECT *, CASE WHEN path LIKE '/.claude/%' THEN '/.claude'
                   WHEN path LIKE '/.codex/%' THEN '/.codex' ELSE 'other' END AS root
    FROM snapshot_files
)
SELECT json_build_object(
    'observed_at', now(), 'machine', l.machine_id,
    'latest_inventoried_capture', l.captured_at,
    'inventory_kind', CASE WHEN l.comparison_snapshot_id IS NULL THEN 'full' ELSE 'diff' END,
    'root', f.root,
    'status', f.status, 'files', count(*), 'bytes', sum(f.byte_size))
FROM latest l
JOIN files f USING (machine_id, snapshot_id)
GROUP BY l.machine_id, l.captured_at, l.comparison_snapshot_id, f.root, f.status
ORDER BY l.machine_id, f.root, f.status;

-- Count latest physical-file versions, not every retained historical version.
WITH latest AS (
    SELECT DISTINCT ON (session_id)
           session_id, complete, parse_issue_count, record_count
    FROM file_versions
    ORDER BY session_id, last_seen_at DESC, first_seen_at DESC, id DESC
)
SELECT json_build_object(
    'observed_at', now(), 'machine', s.machine_id, 'harness', s.harness,
    'latest_version_complete', l.complete, 'sessions', count(*),
    'records', sum(l.record_count), 'parse_issues', sum(l.parse_issue_count))
FROM latest l
JOIN sessions s ON s.id = l.session_id
GROUP BY s.machine_id, s.harness, l.complete
ORDER BY s.machine_id, s.harness, l.complete;
