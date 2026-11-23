-- +goose Up
-- Keep the legacy spelling writable and readable until numeric-only replicas
-- no longer need to overlap with or roll back to text-writing replicas.
-- Stage 31 mirrors every new write. Check that older mappable rows were
-- backfilled before applications can use numeric predicates.
-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM dispatch
        WHERE operation_value IS DISTINCT FROM dispatch_operation_value(operation)
            OR state_value IS DISTINCT FROM dispatch_state_value(state)
    ) OR EXISTS (
        SELECT 1 FROM dispatch_tombstone
        WHERE operation_value IS DISTINCT FROM dispatch_operation_value(operation)
            OR state_value IS DISTINCT FROM dispatch_state_value(state)
    ) OR EXISTS (
        SELECT 1 FROM session
        WHERE state_value IS DISTINCT FROM session_materialization_value(state)
    ) THEN
        RAISE EXCEPTION 'Dispatcher numeric backfill is incomplete; run CALL backfill_dispatch_enum_values(10000) before applying migration 32';
    END IF;
END
$$;
-- +goose StatementEnd

-- +goose StatementBegin
CREATE FUNCTION reconcile_dispatcher_enum(
    field_name text,
    old_text text,
    old_value integer,
    new_text text,
    new_value integer,
    mapped_value integer,
    mapped_text text,
    OUT spelling text,
    OUT value integer
) LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    spelling := new_text;
    value := new_value;
    IF new_text IS DISTINCT FROM old_text THEN
        IF new_value IS DISTINCT FROM old_value AND new_value IS DISTINCT FROM mapped_value THEN
            RAISE EXCEPTION 'inconsistent text and number for %: % and %', field_name, new_text, new_value
                USING ERRCODE = 'check_violation';
        END IF;
        -- Unrecognized legacy strings have no number. In particular, they must
        -- remain distinguishable from the literal Dispatch UNKNOWN state.
        value := mapped_value;
    ELSIF new_value IS DISTINCT FROM old_value THEN
        IF mapped_text IS NULL THEN
            RAISE EXCEPTION 'no legacy spelling for % number %', field_name, new_value
                USING ERRCODE = 'check_violation';
        END IF;
        spelling := mapped_text;
    END IF;
END
$$;
-- +goose StatementEnd

DROP TRIGGER check_dispatch_operation_value ON dispatch;
DROP TRIGGER check_dispatch_operation_value ON dispatch_tombstone;
DROP TRIGGER check_dispatch_state_value ON dispatch;
DROP TRIGGER check_dispatch_state_value ON dispatch_tombstone;
DROP TRIGGER check_session_state_value ON session;
DROP FUNCTION check_dispatch_operation_value();
DROP FUNCTION check_dispatch_state_value();
DROP FUNCTION check_session_state_value();

-- Trigger names are ordered alphabetically. Derive legacy text first so the
-- version triggers see numeric-only changes as changes to the logical row.
ALTER TRIGGER mirror_dispatch_enum_values ON dispatch RENAME TO a_sync_dispatch_enum_values;
ALTER TRIGGER mirror_dispatch_enum_values ON dispatch_tombstone RENAME TO a_sync_dispatch_enum_values;
ALTER TRIGGER mirror_session_state_value ON session RENAME TO a_sync_session_state_value;

-- The gate guarantees there are no remaining metadata-only fills. Restore the
-- original business-write triggers, including versions for value-preserving
-- updates such as Session activity and dispatch retry bookkeeping.
DROP TRIGGER dispatch_assign_version ON dispatch;
DROP TRIGGER dispatch_assign_version_update ON dispatch;
CREATE TRIGGER dispatch_assign_version BEFORE INSERT OR UPDATE ON dispatch
    FOR EACH ROW EXECUTE FUNCTION dispatch_assign_version();

DROP TRIGGER assign_session_watch_version ON session;
DROP TRIGGER assign_session_watch_version_update ON session;
CREATE TRIGGER session_watch_write BEFORE INSERT OR UPDATE ON session
    FOR EACH ROW EXECUTE FUNCTION version_session_write();

-- +goose StatementBegin
CREATE OR REPLACE FUNCTION mirror_dispatch_enum_values() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT spelling, value INTO NEW.operation, NEW.operation_value
    FROM reconcile_dispatcher_enum(
        TG_TABLE_NAME || '.operation', OLD.operation, OLD.operation_value, NEW.operation, NEW.operation_value,
        dispatch_operation_value(NEW.operation),
        (SELECT spelling FROM dispatch_operation_enum() WHERE value = NEW.operation_value)
    );
    SELECT spelling, value INTO NEW.state, NEW.state_value
    FROM reconcile_dispatcher_enum(
        TG_TABLE_NAME || '.state', OLD.state, OLD.state_value, NEW.state, NEW.state_value,
        dispatch_state_value(NEW.state),
        (SELECT spelling FROM dispatch_state_enum() WHERE value = NEW.state_value)
    );
    RETURN NEW;
END
$$;
-- +goose StatementEnd

-- +goose StatementBegin
CREATE OR REPLACE FUNCTION mirror_session_state_value() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT spelling, value INTO NEW.state, NEW.state_value
    FROM reconcile_dispatcher_enum(
        'session.state', OLD.state, OLD.state_value, NEW.state, NEW.state_value,
        session_materialization_value(NEW.state),
        (SELECT spelling FROM session_materialization_enum() WHERE value = NEW.state_value)
    );
    RETURN NEW;
END
$$;
-- +goose StatementEnd

-- The existing dispatch_retain_deletion trigger still copies the original
-- text and allocates one new receipt version. The tombstone trigger populates
-- the corresponding numbers from that spelling, including NULL for unknowns.
