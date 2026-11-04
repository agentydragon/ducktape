-- +goose Up
-- Fresh schema or the completed version-9 predecessor; no partial legacy history is adopted.
-- +goose StatementBegin
DO $actions_baseline$
DECLARE previous_version bigint;
BEGIN
    SELECT COALESCE(max(version_id) FILTER (WHERE is_applied), 0) INTO previous_version
    FROM (SELECT DISTINCT ON (version_id) version_id, is_applied FROM action_service_goose_version ORDER BY version_id, id DESC) latest;
    IF previous_version = 9 THEN
        IF EXISTS (SELECT 1 FROM action_service_goose_version WHERE version_id > 9) OR
            (SELECT count(*) FROM (SELECT DISTINCT ON (version_id) version_id, is_applied FROM action_service_goose_version ORDER BY version_id, id DESC) latest WHERE version_id IN (7, 8, 9) AND is_applied) <> 3 OR
            (SELECT count(*) FROM (SELECT DISTINCT ON (version_id) version_id, is_applied FROM action_service_goose_version ORDER BY version_id, id DESC) latest WHERE version_id BETWEEN 1 AND 6 AND is_applied) NOT IN (0, 6) OR
            (SELECT count(*) FROM (SELECT DISTINCT ON (version_id) version_id, is_applied FROM action_service_goose_version ORDER BY version_id, id DESC) latest WHERE version_id BETWEEN 1 AND 6 AND NOT is_applied) <> 0 OR
            (SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'
            AND table_name IN ('action_request','action_event','action_decision','action_execution','action_executor_heartbeat','action_push_subscription','action_push_delivery','mcp_oauth_token_state','mcp_server_linkage','mcp_linkage_flow')) <> 10
            OR EXISTS (SELECT 1 FROM (VALUES
                ('action_request','caller_uid','text','YES'), ('action_request','action_group','text','NO'),
                ('action_request','action_name','text','NO'), ('action_event','actor_proto','bytea','YES'),
                ('action_decision','policy_evidence_proto','bytea','YES')
            ) AS expected(table_name, column_name, data_type, is_nullable)
            WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns actual WHERE actual.table_schema=current_schema()
                AND actual.table_name=expected.table_name AND actual.column_name=expected.column_name
                AND actual.data_type=expected.data_type AND actual.is_nullable=expected.is_nullable))
            OR (SELECT count(*) FROM pg_index idx JOIN pg_class relation ON relation.oid=idx.indexrelid
                WHERE relation.relnamespace=current_schema()::regnamespace AND relation.relname IN
                ('ix_action_request_caller_created', 'ix_action_request_state_created', 'action_request_history_page',
                 'action_request_pending_page', 'ix_action_execution_lease_sweep', 'ix_action_push_subscription_operator_subject')
                AND idx.indisvalid AND idx.indisready) <> 6
            OR EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name IN ('external_connection','external_connection_grant','connection_enrollment','oauth_registered_client','oauth_authorization','oauth_token'))
            OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='action_request'::regclass AND conname='action_request_caller_uid_nonempty') THEN
            RAISE EXCEPTION 'Action version 9 schema is incomplete; repair it before baseline adoption';
        END IF;
        RETURN;
    END IF;
    IF previous_version <> 0 OR EXISTS (SELECT 1 FROM action_service_goose_version WHERE version_id > 0) OR EXISTS (SELECT 1 FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relkind IN ('r','p','v','m','S','f') AND relname NOT IN ('action_service_goose_version','action_service_goose_version_id_seq')) THEN
        RAISE EXCEPTION 'Action baseline requires a fresh database or completed version 9; run the previous image first';
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
    caller_uid TEXT,
    CONSTRAINT action_request_caller_uid_nonempty CHECK (caller_uid IS NULL OR caller_uid <> ''),
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


COMMENT ON COLUMN action_request.caller_uid IS 'Verified ServiceAccount UID at admission; NULL means the admitting writer did not record it and is never backfilled.';
END
$actions_baseline$;
-- +goose StatementEnd
