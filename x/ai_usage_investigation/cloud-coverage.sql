-- psql: claude_sessions database. Usage content is deliberately not selected.
SELECT json_build_object(
    'sessions', count(*), 'first', min(created_at), 'latest_event', max(last_event_at),
    'caught_up', count(*) FILTER (WHERE synced_last_event_at >= last_event_at))
FROM sessions;

SELECT json_build_object(
    'event_type', event_type, 'n', count(*),
    'first', min(created_at), 'last', max(created_at))
FROM events
GROUP BY event_type
ORDER BY count(*) DESC;
