-- +goose Up
-- Metadata-only DDL. Run the range backfill separately after rollout.
ALTER TABLE dispatch ADD COLUMN operation_value integer, ADD COLUMN state_value integer;
ALTER TABLE dispatch_tombstone ADD COLUMN operation_value integer, ADD COLUMN state_value integer;
ALTER TABLE session ADD COLUMN state_value integer;

-- Pin the public protobuf values here for the lifetime of the text compatibility layer.
-- Unknown spellings are deliberately unmappable. In particular, the
-- Command/Operation COMPACT and UNSPECIFIED members are outside the dispatch vocabulary.
-- +goose StatementBegin
CREATE FUNCTION dispatch_operation_enum() RETURNS TABLE (spelling text, value integer)
IMMUTABLE PARALLEL SAFE LANGUAGE sql AS $$
    VALUES ('submit_input', 1),
           ('change_model', 2),
           ('change_reasoning_effort', 3),
           ('interrupt_turn', 5)
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION dispatch_operation_value(stored text) RETURNS integer
IMMUTABLE STRICT PARALLEL SAFE LANGUAGE sql AS $$
    SELECT value FROM dispatch_operation_enum() WHERE spelling COLLATE "C" = stored COLLATE "C"
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION dispatch_state_enum() RETURNS TABLE (spelling text, value integer)
IMMUTABLE PARALLEL SAFE LANGUAGE sql AS $$
    VALUES ('QUEUED_IN_DISPATCHER', 1),
           ('DELIVERING_TO_RUNNER', 2),
           ('RUNNER_ADMITTED', 3),
           ('FAILED', 6),
           ('CANCELLED_IN_DISPATCHER', 7),
           ('UNKNOWN', 8)
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION dispatch_state_value(stored text) RETURNS integer
IMMUTABLE STRICT PARALLEL SAFE LANGUAGE sql AS $$
    SELECT value FROM dispatch_state_enum() WHERE spelling COLLATE "C" = stored COLLATE "C"
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION session_materialization_enum() RETURNS TABLE (spelling text, value integer)
IMMUTABLE PARALLEL SAFE LANGUAGE sql AS $$
    VALUES ('WAITING', 1),
           ('IN_PROGRESS', 2),
           ('ACCEPTED', 3),
           ('FAILED', 4),
           ('UNKNOWN', 5)
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION session_materialization_value(stored text) RETURNS integer
IMMUTABLE STRICT PARALLEL SAFE LANGUAGE sql AS $$
    SELECT value FROM session_materialization_enum() WHERE spelling COLLATE "C" = stored COLLATE "C"
$$;
-- +goose StatementEnd

-- Validate any explicitly assigned shadow on UPDATE, including NULL. Use UPDATE OF
-- so the check also sees a supplied value equal to the old shadow during a text change.
-- On INSERT, NULL is the default and the mirror fills it.
-- +goose StatementBegin
CREATE FUNCTION check_dispatch_operation_value() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP <> 'INSERT' OR NEW.operation_value IS NOT NULL)
        AND NEW.operation_value IS DISTINCT FROM dispatch_operation_value(NEW.operation) THEN
        RAISE EXCEPTION 'dispatch operation value disagrees with stored text' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION check_dispatch_state_value() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP <> 'INSERT' OR NEW.state_value IS NOT NULL)
        AND NEW.state_value IS DISTINCT FROM dispatch_state_value(NEW.state) THEN
        RAISE EXCEPTION 'dispatch state value disagrees with stored text' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION check_session_state_value() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP <> 'INSERT' OR NEW.state_value IS NOT NULL)
        AND NEW.state_value IS DISTINCT FROM session_materialization_value(NEW.state) THEN
        RAISE EXCEPTION 'Session materialization value disagrees with stored text' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
-- +goose StatementEnd

-- +goose StatementBegin
CREATE FUNCTION mirror_dispatch_enum_values() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.operation_value := dispatch_operation_value(NEW.operation);
    NEW.state_value := dispatch_state_value(NEW.state);
    RETURN NEW;
END
$$;
-- +goose StatementEnd
-- +goose StatementBegin
CREATE FUNCTION mirror_session_state_value() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.state_value := session_materialization_value(NEW.state);
    RETURN NEW;
END
$$;
-- +goose StatementEnd

-- PostgreSQL runs BEFORE triggers alphabetically. Check explicit shadow assignments
-- before version assignment and fill the mirrors after it. This lets old writers
-- keep omitting the shadow fields, including on historically unmappable rows.
CREATE TRIGGER check_dispatch_operation_value BEFORE INSERT OR UPDATE OF operation_value ON dispatch
    FOR EACH ROW EXECUTE FUNCTION check_dispatch_operation_value();
CREATE TRIGGER check_dispatch_state_value BEFORE INSERT OR UPDATE OF state_value ON dispatch
    FOR EACH ROW EXECUTE FUNCTION check_dispatch_state_value();
CREATE TRIGGER mirror_dispatch_enum_values BEFORE INSERT OR UPDATE ON dispatch
    FOR EACH ROW EXECUTE FUNCTION mirror_dispatch_enum_values();

CREATE TRIGGER check_dispatch_operation_value BEFORE INSERT OR UPDATE OF operation_value ON dispatch_tombstone
    FOR EACH ROW EXECUTE FUNCTION check_dispatch_operation_value();
CREATE TRIGGER check_dispatch_state_value BEFORE INSERT OR UPDATE OF state_value ON dispatch_tombstone
    FOR EACH ROW EXECUTE FUNCTION check_dispatch_state_value();
CREATE TRIGGER mirror_dispatch_enum_values BEFORE INSERT OR UPDATE ON dispatch_tombstone
    FOR EACH ROW EXECUTE FUNCTION mirror_dispatch_enum_values();

CREATE TRIGGER check_session_state_value BEFORE INSERT OR UPDATE OF state_value ON session
    FOR EACH ROW EXECUTE FUNCTION check_session_state_value();
CREATE TRIGGER mirror_session_state_value BEFORE INSERT OR UPDATE ON session
    FOR EACH ROW EXECUTE FUNCTION mirror_session_state_value();

-- Reuse the existing version and notification functions. Only a metadata-only fill of
-- one or more NULL shadows is exempt. In particular, an unchanged old writer's UPDATE
-- still creates its usual revision, and backfill never touches the tombstone versions.
DROP TRIGGER dispatch_assign_version ON dispatch;
CREATE TRIGGER dispatch_assign_version BEFORE INSERT ON dispatch
    FOR EACH ROW EXECUTE FUNCTION dispatch_assign_version();
CREATE TRIGGER dispatch_assign_version_update BEFORE UPDATE ON dispatch FOR EACH ROW
    WHEN (
        NOT (
            ((OLD.operation_value IS NULL AND NEW.operation_value IS NOT NULL)
                OR (OLD.state_value IS NULL AND NEW.state_value IS NOT NULL))
            AND (to_jsonb(OLD) - ARRAY['operation_value', 'state_value'])
                = (to_jsonb(NEW) - ARRAY['operation_value', 'state_value'])
        )
    )
    EXECUTE FUNCTION dispatch_assign_version();

DROP TRIGGER session_watch_write ON session;
-- Keep the version trigger before the mirror trigger in alphabetical order.
CREATE TRIGGER assign_session_watch_version BEFORE INSERT ON session
    FOR EACH ROW EXECUTE FUNCTION version_session_write();
CREATE TRIGGER assign_session_watch_version_update BEFORE UPDATE ON session FOR EACH ROW
    WHEN (
        NOT (
            OLD.state_value IS NULL AND NEW.state_value IS NOT NULL
            AND (to_jsonb(OLD) - 'state_value') = (to_jsonb(NEW) - 'state_value')
        )
    )
    EXECUTE FUNCTION version_session_write();

-- +goose StatementBegin
CREATE PROCEDURE backfill_dispatch_enum_values(batch_size integer)
LANGUAGE plpgsql AS $$
DECLARE
    next_command_id varchar;
    next_tombstone_version bigint;
    next_session_id varchar;
BEGIN
    IF batch_size IS NULL OR batch_size <= 0 THEN
        RAISE EXCEPTION 'batch_size must be positive' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- Keyset batches bound the row locks, transaction size, and work on each pass.
    -- The expanded write triggers keep rows inserted or updated concurrently in sync.
    SELECT min(command_id) INTO next_command_id FROM dispatch;
    WHILE next_command_id IS NOT NULL LOOP
        WITH batch AS MATERIALIZED (
            SELECT command_id FROM dispatch
            WHERE command_id >= next_command_id
            ORDER BY command_id LIMIT batch_size
        ), filled AS (
            UPDATE dispatch d
            SET operation_value = dispatch_operation_value(d.operation), state_value = dispatch_state_value(d.state)
            FROM batch
            WHERE d.command_id = batch.command_id
              AND ((d.state_value IS NULL AND dispatch_state_value(d.state) IS NOT NULL)
                  OR (d.operation_value IS NULL AND dispatch_operation_value(d.operation) IS NOT NULL))
        )
        SELECT max(command_id) INTO next_command_id FROM batch;
        COMMIT;
        SELECT min(command_id) INTO next_command_id FROM dispatch WHERE command_id > next_command_id;
    END LOOP;

    SELECT min(version) INTO next_tombstone_version FROM dispatch_tombstone;
    WHILE next_tombstone_version IS NOT NULL LOOP
        WITH batch AS MATERIALIZED (
            SELECT version FROM dispatch_tombstone
            WHERE version >= next_tombstone_version
            ORDER BY version LIMIT batch_size
        ), filled AS (
            UPDATE dispatch_tombstone d
            SET operation_value = dispatch_operation_value(d.operation), state_value = dispatch_state_value(d.state)
            FROM batch
            WHERE d.version = batch.version
              AND ((d.state_value IS NULL AND dispatch_state_value(d.state) IS NOT NULL)
                  OR (d.operation_value IS NULL AND dispatch_operation_value(d.operation) IS NOT NULL))
        )
        SELECT max(version) INTO next_tombstone_version FROM batch;
        COMMIT;
        SELECT min(version) INTO next_tombstone_version FROM dispatch_tombstone WHERE version > next_tombstone_version;
    END LOOP;

    SELECT min(session_id) INTO next_session_id FROM session;
    WHILE next_session_id IS NOT NULL LOOP
        WITH batch AS MATERIALIZED (
            SELECT session_id FROM session
            WHERE session_id >= next_session_id
            ORDER BY session_id LIMIT batch_size
        ), filled AS (
            UPDATE session s SET state_value = session_materialization_value(s.state)
            FROM batch
            WHERE s.session_id = batch.session_id AND s.state_value IS NULL
              AND session_materialization_value(s.state) IS NOT NULL
        )
        SELECT max(session_id) INTO next_session_id FROM batch;
        COMMIT;
        SELECT min(session_id) INTO next_session_id FROM session WHERE session_id > next_session_id;
    END LOOP;
END
$$;
-- +goose StatementEnd
