-- psql: ai_session_archive database; run with PGOPTIONS read-only/timeout.
SELECT json_build_object(
    'machine', machine_id, 'status', processing_status,
    'snapshots', count(*), 'first_capture', min(captured_at),
    'last_capture', max(captured_at))
FROM restic_snapshots
GROUP BY machine_id, processing_status
ORDER BY machine_id, processing_status;

SELECT json_build_object(
    'machine', machine_id, 'harness', harness, 'sessions', count(*),
    'first_activity', min(started_at), 'last_activity', max(last_activity_at))
FROM sessions
GROUP BY machine_id, harness
ORDER BY machine_id, harness;

SELECT json_build_object(
    'table', relname, 'estimated_rows', n_live_tup)
FROM pg_stat_user_tables
WHERE relname IN ('file_versions', 'record_payloads', 'file_version_records');
