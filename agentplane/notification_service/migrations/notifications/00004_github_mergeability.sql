-- +goose Up
CREATE TABLE github_pull_request (
    repository_id BIGINT NOT NULL REFERENCES github_repository (repository_id),
    number BIGINT NOT NULL CHECK (number > 0),
    base_ref VARCHAR NOT NULL,
    head_sha VARCHAR NOT NULL,
    base_sha VARCHAR NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('OPEN', 'CLOSED', 'MERGED')),
    mergeability VARCHAR NOT NULL CHECK (mergeability IN ('MERGEABLE', 'CONFLICTING', 'UNKNOWN')),
    last_settled_mergeability VARCHAR NOT NULL CHECK (last_settled_mergeability IN ('MERGEABLE', 'CONFLICTING', 'UNKNOWN')),
    snapshot_version BIGINT NOT NULL DEFAULT 1,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (repository_id, number)
);
CREATE INDEX ix_github_pull_request_base ON github_pull_request (repository_id, base_ref);

CREATE TABLE github_mergeability_event (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    pull_request_number BIGINT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (repository_id, pull_request_number) REFERENCES github_pull_request (repository_id, number)
);
CREATE INDEX ix_github_mergeability_event_subject
    ON github_mergeability_event (app_id, repository_id, pull_request_number, sequence);

CREATE INDEX ix_github_mergeability_event_created_at ON github_mergeability_event (created_at, sequence);

ALTER TABLE subscription
    ADD COLUMN github_mergeability_after_sequence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN github_mergeability_initialized BOOLEAN NOT NULL DEFAULT false;
UPDATE subscription SET github_mergeability_initialized = true WHERE github_subject_kind IS DISTINCT FROM 'pull_request';
