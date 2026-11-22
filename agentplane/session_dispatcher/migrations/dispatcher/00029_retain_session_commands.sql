-- +goose Up
-- Older replicas delete every dispatch for the UID after fencing the Sandbox and
-- settling delivery. Suppress that DELETE while the exact Session owns the row.
-- +goose StatementBegin
CREATE FUNCTION keep_session_command_on_environment_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    first_cleanup boolean;
BEGIN
    SELECT s.environment_deleted_at IS NULL INTO first_cleanup
    FROM session s JOIN deleted_sandbox e ON e.sandbox_uid=s.sandbox_uid
    WHERE (s.session_id,s.sandbox_name,s.sandbox_uid)=(OLD.session_id,OLD.sandbox_name,OLD.sandbox_uid);
    IF FOUND THEN
        -- Older writers notify only when DELETE reports a nonzero row count.
        IF first_cleanup THEN
            PERFORM pg_notify('agentplane_dispatcher_dispatch', OLD.session_id);
        END IF;
        RETURN NULL;
    END IF;
    RETURN OLD;
END
$$;
-- +goose StatementEnd
CREATE TRIGGER keep_session_command_on_environment_deletion BEFORE DELETE ON dispatch
    FOR EACH ROW EXECUTE FUNCTION keep_session_command_on_environment_deletion();
