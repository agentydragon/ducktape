-- +goose Up
-- Fresh databases create the final typed schema. A completed version-6 database is adopted in place.
-- Versions 1-6 remain in Goose history, even though their upgrade code is retired from this image.
-- +goose StatementBegin
DO $action_final_schema$
DECLARE
    applied_versions INTEGER;
    latest_version BIGINT;
BEGIN
    IF to_regclass(format('%I.%I', current_schema(), 'action_service_goose_version')) IS NULL THEN
        RAISE EXCEPTION 'Action Goose history is missing';
    END IF;

    WITH latest AS (
        SELECT DISTINCT ON (version_id) version_id, is_applied
        FROM action_service_goose_version
        ORDER BY version_id, id DESC
    )
    SELECT COALESCE(count(*) FILTER (WHERE is_applied AND version_id BETWEEN 1 AND 6), 0),
           COALESCE(max(version_id) FILTER (WHERE is_applied), 0)
    INTO applied_versions, latest_version
    FROM latest;

    IF latest_version = 6 AND applied_versions = 6 THEN
        IF (SELECT count(*) FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
                AND table_name IN ('action_request', 'action_event', 'action_decision', 'action_execution', 'action_executor_heartbeat', 'action_push_subscription', 'action_push_delivery', 'external_connection', 'external_connection_grant', 'connection_enrollment', 'mcp_oauth_token_state', 'mcp_server_linkage', 'mcp_linkage_flow', 'oauth_registered_client', 'oauth_authorization', 'oauth_token')) <> 16
            OR EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND (
                    (table_name = 'action_request' AND column_name = 'action') OR
                    (table_name = 'action_event' AND column_name = 'actor') OR
                    (table_name = 'action_decision' AND column_name = 'policy_evidence') OR
                    (table_name = 'external_connection_grant' AND column_name IN ('request_digest', 'activation_deadline')) OR
                    (table_name = 'connection_enrollment' AND column_name IN ('browser_hash', 'decision_digest'))
                )
            )
            OR (SELECT count(*) FROM pg_index idx
                JOIN pg_class relation ON relation.oid = idx.indexrelid
                WHERE relation.relnamespace = current_schema()::regnamespace
                    AND relation.relname IN ('ix_action_request_caller_created', 'ix_action_request_state_created', 'action_request_history_page', 'action_request_pending_page', 'ix_action_execution_lease_sweep', 'ix_action_push_subscription_operator_subject')
                    AND idx.indisvalid AND idx.indisready) <> 6
            OR EXISTS (
                SELECT 1 FROM (VALUES
                    ('action_request', 'action_group', 'text', 'NO'),
                    ('action_request', 'action_name', 'text', 'NO'),
                    ('action_event', 'actor_proto', 'bytea', 'YES'),
                    ('action_decision', 'policy_evidence_proto', 'bytea', 'YES')
                ) AS expected(table_name, column_name, data_type, is_nullable)
                WHERE NOT EXISTS (
                    SELECT 1 FROM information_schema.columns actual
                    WHERE actual.table_schema = current_schema()
                        AND actual.table_name = expected.table_name
                        AND actual.column_name = expected.column_name
                        AND actual.data_type = expected.data_type
                        AND actual.is_nullable = expected.is_nullable
                )
            )
        THEN
            RAISE EXCEPTION 'Action version 6 schema is incomplete; run the previous image or repair before migration 7';
        END IF;
        RETURN;
    END IF;

    IF latest_version <> 0 OR applied_versions <> 0 THEN
        RAISE EXCEPTION 'Action storage requires completed version 6 before migration 7; run the previous image';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class relation
        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = current_schema()
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
            AND relation.relname NOT IN ('action_service_goose_version', 'action_service_goose_version_id_seq')
    ) THEN
        RAISE EXCEPTION 'Action database contains objects without completed version 6 history';
    END IF;

CREATE TABLE action_request (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    arguments JSONB NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    caller_namespace TEXT NOT NULL,
    caller_name TEXT NOT NULL,
    history_at TIMESTAMPTZ,
    action_group TEXT NOT NULL,
    action_name TEXT NOT NULL,
    CONSTRAINT action_request_caller_idempotency_key UNIQUE (caller_namespace, caller_name, idempotency_key)
);
CREATE INDEX ix_action_request_caller_created
    ON action_request (caller_namespace, caller_name, created_at);
CREATE INDEX ix_action_request_state_created ON action_request (state, created_at);
CREATE INDEX action_request_history_page
    ON action_request (history_at DESC, id DESC) WHERE history_at IS NOT NULL;
CREATE INDEX action_request_pending_page
    ON action_request (created_at DESC, id DESC) WHERE state = 'decision_pending';

CREATE TABLE action_event (
    request_id UUID NOT NULL REFERENCES action_request (id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    actor_proto BYTEA,
    PRIMARY KEY (request_id, sequence)
);

CREATE TABLE action_decision (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE REFERENCES action_request (id) ON DELETE CASCADE,
    verdict TEXT NOT NULL,
    provider TEXT NOT NULL,
    decision_note TEXT,
    idempotency_key TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL,
    reason_code TEXT,
    reason_description TEXT,
    operator_issuer TEXT,
    operator_subject TEXT,
    policy_evidence_proto BYTEA,
    CONSTRAINT action_decision_provider_operator_idempotency_key
        UNIQUE NULLS NOT DISTINCT (provider, operator_issuer, operator_subject, idempotency_key),
    CONSTRAINT action_decision_operator_whole CHECK ((operator_issuer IS NULL) = (operator_subject IS NULL))
);

CREATE TABLE action_execution (
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
CREATE INDEX ix_action_execution_lease_sweep
    ON action_execution (lease_expires_at) WHERE state IN ('dispatching', 'running');

CREATE TABLE action_executor_heartbeat (
    executor_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE action_push_subscription (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    operator_issuer TEXT NOT NULL,
    operator_subject TEXT NOT NULL
);
CREATE INDEX ix_action_push_subscription_operator_subject
    ON action_push_subscription (operator_subject);

CREATE TABLE action_push_delivery (
    request_id UUID NOT NULL REFERENCES action_request (id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL REFERENCES action_push_subscription (endpoint) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    PRIMARY KEY (request_id, endpoint)
);

CREATE TABLE external_connection (
    id UUID PRIMARY KEY,
    display_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    bound_caller JSONB,
    binding_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE external_connection_grant (
    id UUID PRIMARY KEY,
    connection_id UUID NOT NULL REFERENCES external_connection (id),
    revision INTEGER NOT NULL,
    issuer TEXT NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    original_caller JSONB NOT NULL,
    UNIQUE (connection_id, revision)
);

CREATE TABLE connection_enrollment (
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
    operator_issuer TEXT,
    operator_subject TEXT,
    verdict TEXT,
    display_name TEXT,
    exchange_claimed_at TIMESTAMPTZ,
    connection_id UUID REFERENCES external_connection (id),
    connection_version INTEGER,
    caller JSONB,
    UNIQUE (client_id, redirect_uri, code_challenge)
);

CREATE TABLE mcp_oauth_token_state (
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

CREATE TABLE mcp_server_linkage (
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

CREATE TABLE mcp_linkage_flow (
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

CREATE TABLE oauth_registered_client (
    client_id TEXT PRIMARY KEY,
    metadata JSONB NOT NULL
);

-- An authorization request and the code its upstream sign-in earns. It asks consent through the enrollment of the
-- same id, whose grant the code exchange creates under that id too.
CREATE TABLE oauth_authorization (
    id UUID PRIMARY KEY REFERENCES connection_enrollment (id),
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    -- The client's state, returned with the code; NULL when it sent none.
    state TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    -- The upstream leg's state and PKCE verifier.
    upstream_state_hash TEXT NOT NULL UNIQUE,
    upstream_verifier TEXT NOT NULL,
    -- Set by the upstream sign-in of the pinned operator: the code, and that operator's upstream session, which a
    -- refresh renews.
    code_hash TEXT UNIQUE,
    signed_in_at TIMESTAMPTZ,
    upstream_expires_at TIMESTAMPTZ,
    upstream_refresh_token TEXT,
    exchanged_at TIMESTAMPTZ,
    CONSTRAINT oauth_authorization_signed_in_whole CHECK (
        (code_hash IS NULL) = (signed_in_at IS NULL) AND (code_hash IS NULL) = (upstream_expires_at IS NULL)
    ),
    CONSTRAINT oauth_authorization_exchanged_after_sign_in CHECK (exchanged_at IS NULL OR code_hash IS NOT NULL)
);

-- An access or refresh token of the authorization's family.
CREATE TABLE oauth_token (
    token_hash TEXT PRIMARY KEY,
    authorization_id UUID NOT NULL REFERENCES oauth_authorization (id),
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    expires_at TIMESTAMPTZ NOT NULL,
    -- When a refresh spent the refresh token; NULL while it is unspent, and always for an access token.
    rotated_at TIMESTAMPTZ,
    CONSTRAINT oauth_token_rotated_refresh CHECK (kind = 'refresh' OR rotated_at IS NULL)
);
END
$action_final_schema$;
-- +goose StatementEnd
