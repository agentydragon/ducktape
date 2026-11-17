-- +goose Up
-- Migration 20 made Session-keyed rows readable while old writers still use the
-- Sandbox-derived key. Rewrite the key in place; dependent rows stay attached to
-- the same inbox ID, and legacy destination columns remain until contract.
LOCK TABLE inbox IN SHARE ROW EXCLUSIVE MODE;

-- +goose StatementBegin
DO $session_inbox_key_preflight$
DECLARE
    unkeyed_count bigint;
    unkeyed_ids text;
BEGIN
    SELECT count(*), string_agg(id::text, ', ' ORDER BY id)
    INTO unkeyed_count, unkeyed_ids
    FROM inbox
    WHERE session_id IS NULL OR btrim(session_id) = '';

    IF unkeyed_count > 0 THEN
        RAISE EXCEPTION 'cannot move notification inboxes to the Session key: % row(s) have no derivable Session ID; inbox IDs: %',
            unkeyed_count, unkeyed_ids;
    END IF;
END
$session_inbox_key_preflight$;
-- +goose StatementEnd

UPDATE inbox
SET destination_key = session_id
WHERE destination_key IS DISTINCT FROM session_id;
