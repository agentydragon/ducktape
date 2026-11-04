-- +goose Up
-- A Session the fold may be behind on: its history row was created, or its committed prefix or feed state
-- changed, since the fold last caught up at `version`. The trigger, not `Append`, maintains it, so the
-- previous release's writers mark Sessions while the two releases run side by side.
CREATE SEQUENCE session_fold_pending_version;
CREATE TABLE session_fold_pending (
    session_id UUID PRIMARY KEY REFERENCES session_history (id) ON DELETE CASCADE,
    version BIGINT NOT NULL
);

-- +goose StatementBegin
CREATE FUNCTION mark_session_fold_pending() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Updating rather than ignoring a pending row waits on a fold deleting it, and a new version keeps the
    -- fold from deleting a row it claimed before this change.
    INSERT INTO session_fold_pending (session_id, version)
        VALUES (NEW.id, nextval('session_fold_pending_version'))
        ON CONFLICT (session_id) DO UPDATE SET version = EXCLUDED.version;
    RETURN NULL;
END $$;
-- +goose StatementEnd

CREATE TRIGGER session_history_fold_pending_insert AFTER INSERT ON session_history
    FOR EACH ROW EXECUTE FUNCTION mark_session_fold_pending();
CREATE TRIGGER session_history_fold_pending_update AFTER UPDATE OF last_cursor, feed_state ON session_history
    FOR EACH ROW WHEN (OLD.last_cursor IS DISTINCT FROM NEW.last_cursor OR OLD.feed_state IS DISTINCT FROM NEW.feed_state)
    EXECUTE FUNCTION mark_session_fold_pending();

-- Every retained Session, so the first fold after this migration covers whatever the previous release
-- left unfolded.
INSERT INTO session_fold_pending (session_id, version)
    SELECT id, nextval('session_fold_pending_version') FROM session_history;

-- +goose Down
DROP TRIGGER session_history_fold_pending_update ON session_history;
DROP TRIGGER session_history_fold_pending_insert ON session_history;
DROP FUNCTION mark_session_fold_pending();
DROP TABLE session_fold_pending;
DROP SEQUENCE session_fold_pending_version;
