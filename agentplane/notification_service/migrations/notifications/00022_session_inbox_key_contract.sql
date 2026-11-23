-- +goose Up
-- Migration 21 keyed every inbox on its Session ID. This retires the
-- Sandbox-derived key and the Sandbox destination and owner columns that
-- carried it, completing the Session-key contract.
--
-- The legacy UNIQUE (owner_namespace, owner_name, destination_key) is redundant:
-- migration 21 set destination_key = session_id and inbox_session_id_unique
-- already refuses a second inbox for a Session. Drop it before the columns.
LOCK TABLE inbox IN SHARE ROW EXCLUSIVE MODE;

-- +goose StatementBegin
DO $session_inbox_key_contract$
DECLARE
    unkeyed_count bigint;
    unkeyed_ids text;
    rekeyed_count bigint;
    rekeyed_ids text;
BEGIN
    -- A row with no Session ID has no key the contract keeps, so refuse rather
    -- than discard how it was addressed.
    SELECT count(*), string_agg(id::text, ', ' ORDER BY id)
    INTO unkeyed_count, unkeyed_ids
    FROM inbox
    WHERE session_id IS NULL OR btrim(session_id) = '';

    IF unkeyed_count > 0 THEN
        RAISE EXCEPTION 'cannot retire the Sandbox-derived notification inbox key: % row(s) have no derivable Session ID; inbox IDs: %',
            unkeyed_count, unkeyed_ids;
    END IF;

    -- A writer predating the cutover derives its key from the Session's Sandbox and
    -- can repopulate it after migration 21. That key carries nothing the Session ID
    -- does not already name, so key the row again and name it, rather than gate the
    -- contract step on clearing what a compatible writer wrote.
    SELECT count(*), string_agg(id::text, ', ' ORDER BY id)
    INTO rekeyed_count, rekeyed_ids
    FROM inbox
    WHERE destination_key IS DISTINCT FROM session_id;

    IF rekeyed_count > 0 THEN
        RAISE NOTICE 'retiring the Sandbox-derived notification inbox key: % row(s) were keyed otherwise than by their Session ID; inbox IDs: %',
            rekeyed_count, rekeyed_ids;
        UPDATE inbox
        SET destination_key = session_id
        WHERE destination_key IS DISTINCT FROM session_id;
    END IF;
END
$session_inbox_key_contract$;
-- +goose StatementEnd

ALTER TABLE inbox
    DROP CONSTRAINT inbox_owner_namespace_owner_name_destination_key_key,
    DROP CONSTRAINT inbox_destination_fields_valid,
    DROP COLUMN destination_key,
    DROP COLUMN destination_namespace,
    DROP COLUMN destination_name,
    DROP COLUMN destination_uid,
    DROP COLUMN owner_namespace,
    DROP COLUMN owner_name;
