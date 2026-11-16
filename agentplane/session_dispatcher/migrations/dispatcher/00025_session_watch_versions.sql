-- +goose Up
CREATE TABLE session_watch_version (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version bigint NOT NULL CHECK (version >= 0)
);
INSERT INTO session_watch_version (version) VALUES (0);
ALTER TABLE session ADD COLUMN version bigint NOT NULL DEFAULT 0;
CREATE INDEX session_version_idx ON session (version);

-- +goose StatementBegin
CREATE FUNCTION version_session_write() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- The counter's row lock lasts until commit. A sequence alone lets a later
    -- version commit first and advance a reader past an uncommitted change.
    UPDATE session_watch_version SET version = version + 1
        WHERE singleton RETURNING version INTO NEW.version;
    PERFORM pg_notify('agentplane_dispatcher_session', 'all');
    PERFORM pg_notify('agentplane_dispatcher_session',
        'identity:' || NEW.execution_identity_name || ':' || NEW.execution_identity_uid::text);
    RETURN NEW;
END;
$$;
-- +goose StatementEnd
CREATE TRIGGER session_watch_write BEFORE INSERT OR UPDATE ON session
    FOR EACH ROW EXECUTE FUNCTION version_session_write();
-- Assign retained Sessions versions through the same writer path.
UPDATE session SET version = version;
