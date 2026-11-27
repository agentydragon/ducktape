-- +goose Up
CREATE TABLE execution_failure_acknowledgement (
    thread_id uuid NOT NULL,
    operator_issuer text NOT NULL CHECK (operator_issuer <> ''),
    operator_subject text NOT NULL CHECK (operator_subject <> ''),
    command_id text NOT NULL CHECK (octet_length(command_id) BETWEEN 1 AND 1024),
    outcome_cursor bigint NOT NULL CHECK (outcome_cursor > 0),
    acknowledged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (thread_id, operator_issuer, operator_subject, command_id, outcome_cursor)
);
