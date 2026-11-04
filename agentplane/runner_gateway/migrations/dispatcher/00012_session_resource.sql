-- +goose Up
-- Keep every retained Session and its immutable spec; this is a relation rename, not replay.
ALTER TABLE session_opening RENAME TO session;
ALTER INDEX ix_session_opening_sandbox_uid RENAME TO ix_session_sandbox_uid;
UPDATE session SET state = CASE state
    WHEN 'QUEUED_IN_DISPATCHER' THEN 'WAITING'
    WHEN 'DELIVERING_TO_RUNNER' THEN 'IN_PROGRESS'
    WHEN 'RUNNER_ADMITTED' THEN 'ACCEPTED'
    ELSE state END;
ALTER TABLE session ADD CONSTRAINT session_materialization_state CHECK
    (state IN ('WAITING', 'IN_PROGRESS', 'ACCEPTED', 'FAILED', 'UNKNOWN'));

-- +goose Down
ALTER TABLE session DROP CONSTRAINT session_materialization_state;
UPDATE session SET state = CASE state
    WHEN 'WAITING' THEN 'QUEUED_IN_DISPATCHER'
    WHEN 'IN_PROGRESS' THEN 'DELIVERING_TO_RUNNER'
    WHEN 'ACCEPTED' THEN 'RUNNER_ADMITTED'
    ELSE state END;
ALTER INDEX ix_session_sandbox_uid RENAME TO ix_session_opening_sandbox_uid;
ALTER TABLE session RENAME TO session_opening;
