-- +goose Up
-- A removal's position orders the Session's removals by commit, so a follower reads only those after
-- the last it reported instead of re-reading every removal on each catch-up.
ALTER TABLE session_event_removed_range ADD COLUMN position BIGINT;

-- Existing rows recorded no insertion order. Their relative order does not matter to followers: every
-- stream opened after this upgrade reads its Session's removals from position 0.
UPDATE session_event_removed_range removed SET position = numbered.position
FROM (
    SELECT session_id, first_cursor, row_number() OVER (PARTITION BY session_id ORDER BY first_cursor) AS position
    FROM session_event_removed_range
) numbered
WHERE removed.session_id = numbered.session_id AND removed.first_cursor = numbered.first_cursor;

ALTER TABLE session_event_removed_range ALTER COLUMN position SET NOT NULL;
ALTER TABLE session_event_removed_range ADD CONSTRAINT removed_range_position UNIQUE (session_id, position);

-- Every writer draws the next position under the Session's history row lock, so positions commit in
-- order per Session and a follower past position P never misses a later commit below it.
-- +goose StatementBegin
CREATE FUNCTION assign_removed_range_position() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM FROM session_history WHERE id = NEW.session_id FOR UPDATE;
    NEW.position := COALESCE(
        (SELECT max(position) FROM session_event_removed_range WHERE session_id = NEW.session_id), 0) + 1;
    RETURN NEW;
END $$;
-- +goose StatementEnd
CREATE TRIGGER session_event_removed_range_position BEFORE INSERT ON session_event_removed_range
    FOR EACH ROW EXECUTE FUNCTION assign_removed_range_position();

-- +goose Down
DROP TRIGGER session_event_removed_range_position ON session_event_removed_range;
DROP FUNCTION assign_removed_range_position();
ALTER TABLE session_event_removed_range DROP COLUMN position;
