-- +goose Up
CREATE TABLE github_pull_request (
    repository_id BIGINT NOT NULL REFERENCES github_repository (repository_id),
    number BIGINT NOT NULL CHECK (number > 0),
    base_ref VARCHAR NOT NULL,
    head_sha VARCHAR NOT NULL,
    base_sha VARCHAR NOT NULL,
    open BOOLEAN NOT NULL,
    pending BOOLEAN NOT NULL,
    mergeable BOOLEAN,
    event_sequence BIGINT NOT NULL DEFAULT 0,
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
    FOREIGN KEY (repository_id, pull_request_number) REFERENCES github_pull_request (repository_id, number)
);
CREATE INDEX ix_github_mergeability_event_subject
    ON github_mergeability_event (app_id, repository_id, pull_request_number, sequence);

ALTER TABLE subscription
    ADD COLUMN github_mergeability_after_sequence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN github_mergeability_initialized BOOLEAN NOT NULL DEFAULT false;
UPDATE subscription SET github_mergeability_initialized = true WHERE github_subject_kind IS DISTINCT FROM 'pull_request';
