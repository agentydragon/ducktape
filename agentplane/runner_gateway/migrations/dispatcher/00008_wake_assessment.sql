-- +goose Up
-- WAKE_STATE_NOT_EVALUATED is encoded as field 1, enum value 1. Keep the
-- default so older Gateway replicas can omit wake_assessment during rollout.
ALTER TABLE dispatch
    ADD COLUMN delivery_blocker BYTEA,
    ADD COLUMN wake_assessment BYTEA NOT NULL DEFAULT decode('0801', 'hex');

ALTER TABLE session_opening
    ADD COLUMN delivery_blocker BYTEA,
    ADD COLUMN wake_assessment BYTEA NOT NULL DEFAULT decode('0801', 'hex');

-- +goose Down
ALTER TABLE session_opening
    DROP COLUMN wake_assessment,
    DROP COLUMN delivery_blocker;

ALTER TABLE dispatch
    DROP COLUMN wake_assessment,
    DROP COLUMN delivery_blocker;
