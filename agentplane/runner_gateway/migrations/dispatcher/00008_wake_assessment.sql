-- +goose Up
ALTER TABLE dispatch
    ADD COLUMN delivery_blocker BYTEA,
    ADD COLUMN wake_assessment BYTEA;

ALTER TABLE session_opening
    ADD COLUMN delivery_blocker BYTEA,
    ADD COLUMN wake_assessment BYTEA;

-- WAKE_STATE_NOT_EVALUATED = 1 is the protobuf encoding for field 1, enum value 1.
UPDATE dispatch SET wake_assessment = decode('0801', 'hex') WHERE wake_assessment IS NULL;
UPDATE session_opening SET wake_assessment = decode('0801', 'hex') WHERE wake_assessment IS NULL;
ALTER TABLE dispatch ALTER COLUMN wake_assessment SET NOT NULL;
ALTER TABLE session_opening ALTER COLUMN wake_assessment SET NOT NULL;

-- +goose Down
ALTER TABLE session_opening
    DROP COLUMN wake_assessment,
    DROP COLUMN delivery_blocker;

ALTER TABLE dispatch
    DROP COLUMN wake_assessment,
    DROP COLUMN delivery_blocker;
