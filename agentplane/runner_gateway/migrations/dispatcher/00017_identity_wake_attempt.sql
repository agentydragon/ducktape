-- +goose Up
-- The wake budget of a caller whose workload SVID names an Identity, keyed by that Identity
-- incarnation. A replica that predates it never wakes for an Identity caller.
CREATE TABLE identity_wake_attempt (
    identity_name VARCHAR NOT NULL,
    identity_uid VARCHAR NOT NULL,
    sandbox_uid UUID NOT NULL,
    wake_generation BIGINT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (identity_name, identity_uid, sandbox_uid, wake_generation)
);
CREATE INDEX ix_identity_wake_attempt_caller_time
    ON identity_wake_attempt (identity_name, identity_uid, attempted_at);
