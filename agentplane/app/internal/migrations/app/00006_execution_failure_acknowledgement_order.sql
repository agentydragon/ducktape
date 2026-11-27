-- +goose Up
ALTER TABLE execution_failure_acknowledgement
    ADD COLUMN acknowledgement_id bigint GENERATED ALWAYS AS IDENTITY,
    ADD UNIQUE (acknowledgement_id);
CREATE INDEX execution_failure_acknowledgement_history
    ON execution_failure_acknowledgement (thread_id, operator_issuer, operator_subject, acknowledgement_id DESC);
