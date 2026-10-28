-- +goose Up
-- Keep the committed aggregate CI schema intact for rollback. New code writes the native check-run and
-- commit-status tables; legacy rows are copied with their exact identities and payloads so inbox history and
-- late-subscriber snapshots remain stable.
ALTER TABLE subscription
    ADD COLUMN github_commit_after_sequence BIGINT NOT NULL DEFAULT 0;
UPDATE subscription SET github_commit_after_sequence = github_ci_after_sequence WHERE github_ci_after_sequence <> 0;

ALTER TABLE github_delivery
    ADD COLUMN commit_normalized BOOLEAN NOT NULL DEFAULT false;
UPDATE github_delivery SET commit_normalized = true WHERE ci_normalized;

CREATE TABLE github_commit_refresh (
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    subject_kind VARCHAR NOT NULL CHECK (subject_kind = 'pull_request'),
    subject_key VARCHAR NOT NULL,
    event VARCHAR NOT NULL CHECK (event IN ('check_run', 'status')),
    generation BIGINT NOT NULL,
    last_success_at TIMESTAMPTZ,
    error_kind VARCHAR,
    error VARCHAR,
    error_since TIMESTAMPTZ,
    error_observed_at TIMESTAMPTZ,
    next_attempt TIMESTAMPTZ,
    claim UUID,
    claim_until TIMESTAMPTZ,
    PRIMARY KEY (app_id, installation_id, repository_id, subject_kind, subject_key, event),
    FOREIGN KEY (app_id, installation_id, repository_id)
        REFERENCES github_repository_access (app_id, installation_id, repository_id),
    FOREIGN KEY (repository_id, subject_kind, subject_key)
        REFERENCES github_subject (repository_id, kind, subject_key),
    CONSTRAINT github_commit_refresh_error_kind CHECK (
        error_kind IN ('rate_limited', 'unavailable', 'access_denied', 'source_changed', 'processing_error')),
    CONSTRAINT github_commit_refresh_error_state CHECK (
        (error IS NULL AND error_kind IS NULL AND error_since IS NULL AND error_observed_at IS NULL) OR
        (error IS NOT NULL AND error_kind IS NOT NULL AND error_since IS NOT NULL AND error_observed_at IS NOT NULL))
);

-- The old lease represented both endpoints. Preserve its fencing generation, but make each endpoint due now:
-- an aggregate failure cannot tell us which endpoint has current coverage.
INSERT INTO github_commit_refresh (
    app_id, installation_id, repository_id, subject_kind, subject_key, event, generation
)
SELECT r.app_id, r.installation_id, r.repository_id, r.subject_kind, r.subject_key, e.event, r.generation
FROM github_ci_refresh r
CROSS JOIN (VALUES ('check_run'::VARCHAR), ('status'::VARCHAR)) AS e(event);

CREATE TABLE github_commit_current (
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL REFERENCES github_repository (repository_id),
    head_sha VARCHAR NOT NULL,
    event VARCHAR NOT NULL CHECK (event IN ('check_run', 'status')),
    entity_key VARCHAR NOT NULL,
    last_sequence BIGINT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (app_id, installation_id, repository_id, head_sha, event, entity_key)
);
CREATE INDEX ix_github_commit_current_revision
    ON github_commit_current (app_id, repository_id, head_sha, event);
INSERT INTO github_commit_current
    (app_id, installation_id, repository_id, head_sha, event, entity_key, last_sequence, payload)
SELECT app_id, installation_id, repository_id, head_sha, event, entity_key, last_sequence, payload
FROM github_ci_current;

CREATE TABLE github_commit_event (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL REFERENCES github_repository (repository_id),
    head_sha VARCHAR NOT NULL,
    event VARCHAR NOT NULL CHECK (event IN ('check_run', 'status')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_github_commit_event_revision
    ON github_commit_event (app_id, repository_id, head_sha, event, sequence);
CREATE INDEX ix_github_commit_event_created_at ON github_commit_event (created_at, sequence);
INSERT INTO github_commit_event
    (sequence, app_id, installation_id, repository_id, head_sha, event, payload, created_at)
OVERRIDING SYSTEM VALUE
SELECT sequence, app_id, installation_id, repository_id, head_sha, event, payload, created_at
FROM github_ci_event;
SELECT setval(pg_get_serial_sequence('github_commit_event', 'sequence'),
    GREATEST(
        COALESCE((SELECT MAX(sequence) FROM github_ci_event), 0),
        COALESCE((SELECT MAX(last_sequence) FROM github_ci_current), 0),
        COALESCE((SELECT MAX(sequence) FROM github_ci_subscription_snapshot), 0),
        COALESCE(pg_sequence_last_value(pg_get_serial_sequence('github_ci_event', 'sequence')::regclass), 0),
        1
    ),
    GREATEST(
        COALESCE((SELECT MAX(sequence) FROM github_ci_event), 0),
        COALESCE((SELECT MAX(last_sequence) FROM github_ci_current), 0),
        COALESCE((SELECT MAX(sequence) FROM github_ci_subscription_snapshot), 0),
        COALESCE(pg_sequence_last_value(pg_get_serial_sequence('github_ci_event', 'sequence')::regclass), 0)
    ) > 0
);

CREATE TABLE github_commit_subscription_snapshot (
    subscription_id UUID NOT NULL REFERENCES subscription (id) ON DELETE CASCADE,
    app_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    event VARCHAR NOT NULL CHECK (event IN ('check_run', 'status')),
    sequence BIGINT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (subscription_id, sequence)
);
CREATE INDEX ix_github_commit_subscription_snapshot_page
    ON github_commit_subscription_snapshot (subscription_id, sequence);
INSERT INTO github_commit_subscription_snapshot
    (subscription_id, app_id, installation_id, repository_id, event, sequence, payload)
SELECT subscription_id, app_id, installation_id, repository_id, event, sequence, payload
FROM github_ci_subscription_snapshot;

-- +goose Down
DROP TABLE github_commit_subscription_snapshot;
DROP TABLE github_commit_event;
DROP TABLE github_commit_current;
DROP TABLE github_commit_refresh;
ALTER TABLE github_delivery DROP COLUMN commit_normalized;
ALTER TABLE subscription DROP COLUMN github_commit_after_sequence;
