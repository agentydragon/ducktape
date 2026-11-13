-- +goose Up
CREATE TABLE IF NOT EXISTS egress_decision (
    event_id UUID PRIMARY KEY,
    decided_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    producer_id UUID NOT NULL,
    subject_name TEXT,
    subject_namespace TEXT,
    source_pod_uid TEXT,
    connection_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    method TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    binding TEXT,
    policy TEXT,
    rule INTEGER,
    substituted BOOLEAN NOT NULL,
    address INET,
    CONSTRAINT egress_decision_subject_whole CHECK ((subject_namespace IS NULL) = (subject_name IS NULL))
);
CREATE INDEX IF NOT EXISTS egress_decision_subject_recent
    ON egress_decision (subject_namespace, subject_name, decided_at, event_id);
CREATE INDEX IF NOT EXISTS egress_decision_denials_recent
    ON egress_decision (outcome, decided_at, event_id);
CREATE INDEX IF NOT EXISTS egress_decision_retention
    ON egress_decision (decided_at, event_id);
