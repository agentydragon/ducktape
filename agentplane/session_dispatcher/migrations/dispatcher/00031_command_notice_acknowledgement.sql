-- +goose Up
-- Writer verifies the exact live or legacy receipt before recording attention.
-- Revisions of both sources use one sequence. Retain references after a receipt is superseded or pruned.
CREATE TABLE command_notice_acknowledgement (
    acknowledgement_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id varchar NOT NULL,
    command_id varchar NOT NULL,
    receipt_version bigint NOT NULL CHECK (receipt_version > 0),
    acknowledged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, receipt_version)
);
CREATE INDEX ix_command_notice_acknowledgement_history
    ON command_notice_acknowledgement (session_id, acknowledgement_id DESC);
