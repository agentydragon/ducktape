-- +goose Up
-- Nullable, so replicas still writing the earlier columns keep inserting during a roll. A row's
-- subject is a ServiceAccount, an Identity, or neither when the request never authenticated.
ALTER TABLE egress_decision
    ADD COLUMN IF NOT EXISTS subject_identity TEXT,
    ADD CONSTRAINT egress_decision_subject_one_kind
        CHECK (subject_identity IS NULL OR subject_namespace IS NULL);
CREATE INDEX IF NOT EXISTS egress_decision_identity_recent
    ON egress_decision (subject_identity, decided_at, event_id) WHERE subject_identity IS NOT NULL;
