-- +goose Up
-- The schema Alembic revision 0027_drop_reconciliation_audit left, with columns in the order that history
-- added them. A database that revision built already holds everything below, so every statement is
-- idempotent; that same database must not be behind it, or `IF NOT EXISTS` would pass over a table of an
-- older shape.
-- +goose StatementBegin
DO $$
BEGIN
    IF to_regclass('alembic_version') IS NOT NULL THEN
        IF (SELECT version_num FROM alembic_version) IS DISTINCT FROM '0027_drop_reconciliation_audit' THEN
            RAISE EXCEPTION 'alembic_version is behind 0027_drop_reconciliation_audit: run the Python migration image first';
        END IF;
    END IF;
END $$;
-- +goose StatementEnd

CREATE TABLE IF NOT EXISTS action_request (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    arguments JSONB NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    action JSONB NOT NULL,
    external_grant JSONB,
    title TEXT NOT NULL,
    description TEXT,
    caller_namespace TEXT NOT NULL,
    caller_name TEXT NOT NULL,
    history_at TIMESTAMPTZ,
    CONSTRAINT action_request_caller_idempotency_key UNIQUE (caller_namespace, caller_name, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_action_request_caller_created
    ON action_request (caller_namespace, caller_name, created_at);
CREATE INDEX IF NOT EXISTS ix_action_request_state_created ON action_request (state, created_at);
CREATE INDEX IF NOT EXISTS action_request_history_page
    ON action_request (history_at DESC, id DESC) WHERE history_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS action_request_pending_page
    ON action_request (created_at DESC, id DESC) WHERE state = 'decision_pending';

CREATE TABLE IF NOT EXISTS action_event (
    request_id UUID NOT NULL REFERENCES action_request (id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    actor JSONB,
    PRIMARY KEY (request_id, sequence)
);

CREATE TABLE IF NOT EXISTS action_decision (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE REFERENCES action_request (id) ON DELETE CASCADE,
    verdict TEXT NOT NULL,
    provider TEXT NOT NULL,
    decision_note TEXT,
    idempotency_key TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL,
    reason_code TEXT,
    reason_description TEXT,
    policy_evidence JSONB,
    operator_issuer TEXT,
    operator_subject TEXT,
    CONSTRAINT action_decision_provider_operator_idempotency_key
        UNIQUE NULLS NOT DISTINCT (provider, operator_issuer, operator_subject, idempotency_key),
    CONSTRAINT action_decision_operator_whole CHECK ((operator_issuer IS NULL) = (operator_subject IS NULL))
);

CREATE TABLE IF NOT EXISTS action_execution (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE REFERENCES action_request (id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    result JSONB,
    error JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    executor_id TEXT,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    reconciled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_action_execution_lease_sweep
    ON action_execution (lease_expires_at) WHERE state IN ('dispatching', 'running');

CREATE TABLE IF NOT EXISTS action_executor_heartbeat (
    executor_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS action_push_subscription (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    operator_issuer TEXT NOT NULL,
    operator_subject TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_action_push_subscription_operator_subject
    ON action_push_subscription (operator_subject);

CREATE TABLE IF NOT EXISTS action_push_delivery (
    request_id UUID NOT NULL REFERENCES action_request (id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL REFERENCES action_push_subscription (endpoint) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    PRIMARY KEY (request_id, endpoint)
);

CREATE TABLE IF NOT EXISTS external_connection (
    id UUID PRIMARY KEY,
    display_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    bound_caller JSONB,
    binding_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS external_connection_grant (
    id UUID PRIMARY KEY,
    connection_id UUID NOT NULL REFERENCES external_connection (id),
    revision INTEGER NOT NULL,
    issuer TEXT NOT NULL,
    client_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    activation_deadline TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    original_caller JSONB NOT NULL,
    UNIQUE (connection_id, revision)
);

CREATE TABLE IF NOT EXISTS connection_enrollment (
    id UUID PRIMARY KEY,
    handle_hash TEXT NOT NULL UNIQUE,
    issuer TEXT NOT NULL,
    client_id TEXT NOT NULL,
    client_name TEXT,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    upstream_url TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    version INTEGER NOT NULL,
    browser_hash TEXT,
    operator_issuer TEXT,
    operator_subject TEXT,
    verdict TEXT,
    decision_digest TEXT,
    display_name TEXT,
    exchange_claimed_at TIMESTAMPTZ,
    connection_id UUID REFERENCES external_connection (id),
    connection_version INTEGER,
    caller JSONB,
    UNIQUE (client_id, redirect_uri, code_challenge)
);

CREATE TABLE IF NOT EXISTS mcp_oauth_token_state (
    id UUID PRIMARY KEY,
    server_id TEXT NOT NULL UNIQUE,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_type TEXT NOT NULL,
    scope JSONB NOT NULL,
    expires_at TIMESTAMPTZ,
    token_revision INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    refresh_claim_id UUID,
    refresh_claim_expires_at TIMESTAMPTZ,
    refresh_failure_count INTEGER NOT NULL,
    refresh_failure_started_at TIMESTAMPTZ,
    refresh_failure_latest_at TIMESTAMPTZ,
    refresh_failure_action TEXT,
    refresh_retry_at TIMESTAMPTZ,
    refresh_failure_error TEXT,
    CONSTRAINT mcp_oauth_token_state_refresh_failure_whole
        CHECK ((refresh_failure_action IS NULL) = (refresh_failure_error IS NULL))
);

CREATE TABLE IF NOT EXISTS mcp_server_linkage (
    server_id TEXT PRIMARY KEY,
    server_url TEXT NOT NULL,
    revision INTEGER NOT NULL,
    scopes JSONB NOT NULL,
    token_state_id UUID REFERENCES mcp_oauth_token_state (id) ON DELETE SET NULL,
    token_endpoint TEXT,
    resource TEXT,
    linked_at TIMESTAMPTZ,
    linked_by_issuer TEXT,
    linked_by_subject TEXT,
    CONSTRAINT mcp_server_linkage_linked_by_whole CHECK ((linked_by_issuer IS NULL) = (linked_by_subject IS NULL))
);

CREATE TABLE IF NOT EXISTS mcp_linkage_flow (
    id UUID PRIMARY KEY,
    server_id TEXT NOT NULL,
    state_hash TEXT NOT NULL UNIQUE,
    verifier TEXT NOT NULL,
    scopes JSONB NOT NULL,
    authorization_endpoint TEXT NOT NULL,
    token_endpoint TEXT NOT NULL,
    resource TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    operator_issuer TEXT NOT NULL,
    operator_subject TEXT NOT NULL
);
