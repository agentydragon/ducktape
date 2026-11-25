-- +goose Up
-- Keep writers on the previous sampler version able to wake a new turn owner during rolls.
-- +goose StatementBegin
CREATE FUNCTION notify_sampler_turn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    conversation uuid;
BEGIN
    IF TG_TABLE_NAME = 'sampler_interrupt' THEN
        conversation := NEW.conversation_id;
    ELSE
        SELECT conversation_id INTO STRICT conversation FROM sampler_session WHERE id = NEW.session_id;
    END IF;
    PERFORM pg_notify('central_sampler_turn', conversation::text);
    RETURN NULL;
END;
$$;
-- +goose StatementEnd

CREATE TRIGGER sampler_interrupt_wakeup AFTER INSERT ON sampler_interrupt
    FOR EACH ROW EXECUTE FUNCTION notify_sampler_turn();
CREATE TRIGGER sampler_command_wakeup AFTER INSERT ON sampler_command
    FOR EACH ROW EXECUTE FUNCTION notify_sampler_turn();
