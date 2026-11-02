-- +goose Up
ALTER TABLE wake_attempt ADD COLUMN caller_uid VARCHAR NOT NULL DEFAULT '';
ALTER TABLE wake_attempt DROP CONSTRAINT wake_attempt_pkey;
ALTER TABLE wake_attempt
    ADD CONSTRAINT wake_attempt_pkey
        PRIMARY KEY (caller_namespace, caller_name, caller_uid, sandbox_uid, wake_generation);
DROP INDEX IF EXISTS ix_wake_attempt_caller_time;
CREATE INDEX ix_wake_attempt_caller_time
    ON wake_attempt (caller_namespace, caller_name, caller_uid, attempted_at);

-- +goose Down
DROP INDEX IF EXISTS ix_wake_attempt_caller_time;
ALTER TABLE wake_attempt DROP CONSTRAINT wake_attempt_pkey;
DELETE FROM wake_attempt
WHERE ctid IN (
    SELECT ctid
    FROM (
        SELECT ctid, row_number() OVER (
            PARTITION BY caller_namespace, caller_name, sandbox_uid, wake_generation
            ORDER BY attempted_at DESC, caller_uid DESC
        ) AS duplicate_rank
        FROM wake_attempt
    ) ranked
    WHERE duplicate_rank > 1
);
ALTER TABLE wake_attempt
    ADD CONSTRAINT wake_attempt_pkey
        PRIMARY KEY (caller_namespace, caller_name, sandbox_uid, wake_generation);
ALTER TABLE wake_attempt DROP COLUMN caller_uid;
CREATE INDEX ix_wake_attempt_caller_time
    ON wake_attempt (caller_namespace, caller_name, attempted_at);
