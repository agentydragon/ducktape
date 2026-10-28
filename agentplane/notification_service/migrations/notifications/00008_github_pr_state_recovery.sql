-- +goose Up
ALTER TABLE subscription
    ADD COLUMN github_pr_state_after_sequence BIGINT NOT NULL DEFAULT 0;

ALTER TABLE github_delivery
    ADD COLUMN pr_normalized BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN pr_normalized_action VARCHAR;

CREATE TABLE github_review_refresh (
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    subject_kind VARCHAR NOT NULL CHECK (subject_kind = 'pull_request'),
    subject_key VARCHAR NOT NULL,
    generation BIGINT NOT NULL,
    last_success_at TIMESTAMPTZ,
    error_kind VARCHAR,
    error VARCHAR,
    error_since TIMESTAMPTZ,
    error_observed_at TIMESTAMPTZ,
    next_attempt TIMESTAMPTZ,
    claim UUID,
    claim_until TIMESTAMPTZ,
    PRIMARY KEY (app_id, installation_id, repository_id, subject_kind, subject_key),
    FOREIGN KEY (app_id, installation_id, repository_id)
        REFERENCES github_repository_access (app_id, installation_id, repository_id),
    FOREIGN KEY (repository_id, subject_kind, subject_key)
        REFERENCES github_subject (repository_id, kind, subject_key),
    CONSTRAINT github_review_refresh_error_kind CHECK (
        error_kind IN ('rate_limited', 'unavailable', 'access_denied', 'source_changed', 'processing_error')),
    CONSTRAINT github_review_refresh_error_state CHECK (
        (error IS NULL AND error_kind IS NULL AND error_since IS NULL AND error_observed_at IS NULL) OR
        (error IS NOT NULL AND error_kind IS NOT NULL AND error_since IS NOT NULL AND error_observed_at IS NOT NULL))
);

CREATE TABLE github_pr_state_current (
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL REFERENCES github_repository (repository_id),
    pull_request_number BIGINT NOT NULL,
    event VARCHAR NOT NULL CHECK (event IN ('pull_request', 'pull_request_review')),
    entity_id BIGINT NOT NULL,
    last_sequence BIGINT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (app_id, installation_id, repository_id, pull_request_number, event, entity_id)
);
CREATE INDEX ix_github_pr_state_current_subject
    ON github_pr_state_current (app_id, repository_id, pull_request_number, event);

CREATE TABLE github_pr_state_event (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL REFERENCES github_repository (repository_id),
    pull_request_number BIGINT NOT NULL,
    event VARCHAR NOT NULL CHECK (event IN ('pull_request', 'pull_request_review')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_github_pr_state_event_subject
    ON github_pr_state_event (app_id, repository_id, pull_request_number, event, sequence);
CREATE INDEX ix_github_pr_state_event_created_at ON github_pr_state_event (created_at, sequence);

CREATE TABLE github_pr_state_subscription_snapshot (
    subscription_id UUID NOT NULL REFERENCES subscription (id) ON DELETE CASCADE,
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    pull_request_number BIGINT NOT NULL,
    event VARCHAR NOT NULL CHECK (event IN ('pull_request', 'pull_request_review')),
    sequence BIGINT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (subscription_id, sequence)
);
CREATE INDEX ix_github_pr_state_subscription_snapshot_page
    ON github_pr_state_subscription_snapshot (subscription_id, sequence);

-- +goose Down
DROP TABLE github_pr_state_subscription_snapshot;
DROP TABLE github_pr_state_event;
DROP TABLE github_pr_state_current;
DROP TABLE github_review_refresh;
ALTER TABLE github_delivery DROP COLUMN pr_normalized_action;
ALTER TABLE github_delivery DROP COLUMN pr_normalized;
ALTER TABLE subscription DROP COLUMN github_pr_state_after_sequence;
