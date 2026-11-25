-- +goose Up
-- Apply only after the numeric Dispatcher is fully deployed and rollback to
-- any text-dependent replica has been retired. No Down migration is provided.
-- Keep readers and writers out between validation and the destructive DDL.
LOCK TABLE dispatch, dispatch_tombstone, session IN ACCESS EXCLUSIVE MODE;

-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM dispatch
        WHERE operation_value IS NULL OR state_value IS NULL
            OR operation_value IS DISTINCT FROM dispatch_operation_value(operation)
            OR state_value IS DISTINCT FROM dispatch_state_value(state)
    ) THEN
        RAISE EXCEPTION 'Cannot retire Dispatcher enum text: dispatch has missing, unmapped, or inconsistent values';
    END IF;
    IF EXISTS (
        SELECT 1 FROM dispatch_tombstone
        WHERE operation_value IS NULL OR state_value IS NULL
            OR operation_value IS DISTINCT FROM dispatch_operation_value(operation)
            OR state_value IS DISTINCT FROM dispatch_state_value(state)
    ) THEN
        RAISE EXCEPTION 'Cannot retire Dispatcher enum text: dispatch_tombstone has missing, unmapped, or inconsistent values';
    END IF;
    IF EXISTS (
        SELECT 1 FROM session
        WHERE state_value IS NULL OR state_value IS DISTINCT FROM session_materialization_value(state)
    ) THEN
        RAISE EXCEPTION 'Cannot retire Dispatcher enum text: session has missing, unmapped, or inconsistent values';
    END IF;
END
$$;
-- +goose StatementEnd

DROP TRIGGER a_sync_dispatch_enum_values ON dispatch;
DROP TRIGGER a_sync_dispatch_enum_values ON dispatch_tombstone;
DROP TRIGGER a_sync_session_state_value ON session;

-- Keep the deletion trigger and its receipt shape. Tombstones deliberately
-- omit the command body and Sandbox address, and get a new watch version.
-- +goose StatementBegin
CREATE OR REPLACE FUNCTION dispatch_retain_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO dispatch_tombstone (
        command_id, version, session_id, operation_value, state_value, reason, admission,
        delivery_blocker, wake_assessment, created_at, updated_at
    ) VALUES (
        OLD.command_id, nextval('dispatch_version_seq'), OLD.session_id, OLD.operation_value, OLD.state_value,
        OLD.reason, OLD.admission, OLD.delivery_blocker, OLD.wake_assessment, OLD.created_at, OLD.updated_at
    );
    RETURN OLD;
END
$$;
-- +goose StatementEnd

-- Dropping the text columns also removes their indexes. Keep the numeric
-- indexes from migration 33, and the existing unfiltered indexes.
ALTER TABLE dispatch ALTER COLUMN operation_value SET NOT NULL, ALTER COLUMN state_value SET NOT NULL,
    DROP COLUMN operation, DROP COLUMN state;
ALTER TABLE dispatch_tombstone ALTER COLUMN operation_value SET NOT NULL, ALTER COLUMN state_value SET NOT NULL,
    DROP COLUMN operation, DROP COLUMN state;
ALTER TABLE session ALTER COLUMN state_value SET NOT NULL, DROP COLUMN state;
-- MaterializationState protobuf values WAITING, IN_PROGRESS, ACCEPTED, FAILED, UNKNOWN.
ALTER TABLE session ADD CONSTRAINT session_materialization_state CHECK (state_value IN (1, 2, 3, 4, 5));

DROP PROCEDURE backfill_dispatch_enum_values(integer);
DROP FUNCTION mirror_dispatch_enum_values();
DROP FUNCTION mirror_session_state_value();
DROP FUNCTION reconcile_dispatcher_enum(text, text, integer, text, integer, integer, text);
DROP FUNCTION dispatch_operation_value(text);
DROP FUNCTION dispatch_state_value(text);
DROP FUNCTION session_materialization_value(text);
DROP FUNCTION dispatch_operation_enum();
DROP FUNCTION dispatch_state_enum();
DROP FUNCTION session_materialization_enum();
