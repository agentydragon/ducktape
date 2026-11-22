-- +goose Up
-- Sessions can exist without App-owned title metadata. Dispatcher owns their lifetime.
CREATE TABLE command_notice_acknowledgement (
    acknowledgement_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id uuid NOT NULL,
    operator_issuer text NOT NULL CHECK (operator_issuer <> ''),
    operator_subject text NOT NULL CHECK (operator_subject <> ''),
    command_id text NOT NULL CHECK (octet_length(command_id) BETWEEN 1 AND 1024),
    receipt_version bigint NOT NULL CHECK (receipt_version > 0),
    acknowledged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (thread_id, operator_issuer, operator_subject, command_id, receipt_version)
);
CREATE INDEX command_notice_acknowledgement_history
    ON command_notice_acknowledgement (thread_id, operator_issuer, operator_subject, acknowledgement_id DESC);
