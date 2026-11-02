-- +goose Up
-- WAKE_STATE_NOT_EVALUATED is encoded as field 1, enum value 1. Keep the
-- default so older Gateway replicas can omit wake_assessment during rollout.
ALTER TABLE dispatch
    ADD COLUMN delivery_blocker BYTEA,
    ADD COLUMN wake_assessment BYTEA NOT NULL DEFAULT decode('0801', 'hex');

ALTER TABLE session_opening
    ADD COLUMN delivery_blocker BYTEA,
    ADD COLUMN wake_assessment BYTEA NOT NULL DEFAULT decode('0801', 'hex');

-- Keep caller_uid's default for replicas running the previous INSERT shape.
ALTER TABLE wake_attempt ADD COLUMN caller_uid VARCHAR NOT NULL DEFAULT '';
ALTER TABLE wake_attempt DROP CONSTRAINT wake_attempt_pkey;
ALTER TABLE wake_attempt
    ADD CONSTRAINT wake_attempt_pkey
        PRIMARY KEY (caller_namespace, caller_name, caller_uid, sandbox_uid, wake_generation);
DROP INDEX IF EXISTS ix_wake_attempt_caller_time;
CREATE INDEX ix_wake_attempt_caller_time
    ON wake_attempt (caller_namespace, caller_name, caller_uid, attempted_at);

-- +goose Down
-- The previous key cannot represent duplicate target generations owned by
-- different ServiceAccount UIDs. Refuse downgrade instead of dropping rows.
-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM wake_attempt
        GROUP BY caller_namespace, caller_name, sandbox_uid, wake_generation
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'cannot downgrade wake_attempt while UID-separated generations collide';
    END IF;
END
$$;
-- +goose StatementEnd

DROP INDEX IF EXISTS ix_wake_attempt_caller_time;
ALTER TABLE wake_attempt DROP CONSTRAINT wake_attempt_pkey;
ALTER TABLE wake_attempt
    ADD CONSTRAINT wake_attempt_pkey
        PRIMARY KEY (caller_namespace, caller_name, sandbox_uid, wake_generation);
ALTER TABLE wake_attempt DROP COLUMN caller_uid;
CREATE INDEX ix_wake_attempt_caller_time
    ON wake_attempt (caller_namespace, caller_name, attempted_at);

ALTER TABLE session_opening
    DROP COLUMN wake_assessment,
    DROP COLUMN delivery_blocker;

ALTER TABLE dispatch
    DROP COLUMN wake_assessment,
    DROP COLUMN delivery_blocker;
