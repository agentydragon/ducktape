-- +goose Up
ALTER TABLE session_event ADD COLUMN kind INTEGER;
CREATE INDEX ix_session_event_kind_cursor ON session_event (session_id, kind, cursor);

-- +goose Down
DROP INDEX ix_session_event_kind_cursor;
ALTER TABLE session_event DROP COLUMN kind;
