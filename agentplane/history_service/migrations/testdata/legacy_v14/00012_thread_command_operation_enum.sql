-- +goose Up
-- `ThreadCommand.operation` is the `agentplane.proto.Operation` enum, and Electric readers parse the
-- stored ProtoJSON, so a command row spells the enum value name. Folds before this release wrote the
-- `Command.operation` oneof field name. The trigger also rewrites rows an older replica writes while
-- the rollout overlaps. The removed stop operation keeps its reserved number 4, an unknown value to
-- every reader.
-- CLEANUP(added 2026-11-09): Drop the trigger and function in a new migration once no History
--   replica older than this migration runs (the release carrying it has fully rolled out).
-- +goose StatementBegin
CREATE FUNCTION thread_command_operation_enum() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.entity_kind = 'command' THEN
        NEW.state := CASE NEW.state->>'operation'
            WHEN 'submit_input' THEN jsonb_set(NEW.state, '{operation}', '"OPERATION_SUBMIT_INPUT"')
            WHEN 'change_model' THEN jsonb_set(NEW.state, '{operation}', '"OPERATION_CHANGE_MODEL"')
            WHEN 'change_reasoning_effort' THEN jsonb_set(NEW.state, '{operation}', '"OPERATION_CHANGE_REASONING_EFFORT"')
            WHEN 'interrupt_turn' THEN jsonb_set(NEW.state, '{operation}', '"OPERATION_INTERRUPT_TURN"')
            WHEN 'compact' THEN jsonb_set(NEW.state, '{operation}', '"OPERATION_COMPACT"')
            WHEN 'stop_runner_session' THEN jsonb_set(NEW.state, '{operation}', '4')
            WHEN 'historical_harness_stop' THEN jsonb_set(NEW.state, '{operation}', '4')
            ELSE NEW.state
        END;
    END IF;
    RETURN NEW;
END
$$;
-- +goose StatementEnd
CREATE TRIGGER thread_entity_command_operation_enum BEFORE INSERT OR UPDATE OF state ON thread_entity
    FOR EACH ROW EXECUTE FUNCTION thread_command_operation_enum();
UPDATE thread_entity SET state = state
WHERE entity_kind = 'command' AND jsonb_typeof(state->'operation') = 'string'
  AND state->>'operation' NOT LIKE 'OPERATION\_%';
