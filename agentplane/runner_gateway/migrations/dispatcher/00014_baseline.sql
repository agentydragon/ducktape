-- +goose Up
-- Version 14 replaces the Dispatcher migration chain. A database at version 13 is adopted
-- only after the complete final schema is verified; no retained rows are rewritten.
-- +goose StatementBegin
DO $dispatcher_baseline$
DECLARE
    applied_versions INTEGER;
    latest_version BIGINT;
    existing_tables INTEGER;
BEGIN
    SELECT count(*) INTO existing_tables
    FROM (VALUES ('dispatch'), ('session'), ('session_opening'), ('sandbox_ownership'),
        ('wake_attempt'), ('deleted_sandbox')) AS expected(name)
    WHERE to_regclass(format('%I.%I', current_schema(), expected.name)) IS NOT NULL;

    IF existing_tables = 0 THEN
        -- Goose creates its history table before executing the first migration.
        IF EXISTS (SELECT 1 FROM runner_gateway_dispatcher_goose_version WHERE version_id > 0)
            OR EXISTS (SELECT 1 FROM pg_class WHERE relnamespace=current_schema()::regnamespace
                AND relkind IN ('r','p','v','m','S','f')
                AND relname NOT IN ('runner_gateway_dispatcher_goose_version','runner_gateway_dispatcher_goose_version_id_seq')) THEN
            RAISE EXCEPTION 'Dispatcher history exists without its schema; restore it or use the previous image to finish migration 13';
        END IF;
        RETURN;
    END IF;

    IF existing_tables <> 5
        OR to_regclass(format('%I.%I', current_schema(), 'session_opening')) IS NOT NULL THEN
        RAISE EXCEPTION 'Dispatcher schema is not at the version-13 tip; run the previous image to finish migration 13';
    END IF;

    WITH latest AS (
        SELECT DISTINCT ON (version_id) version_id, is_applied
        FROM runner_gateway_dispatcher_goose_version
        WHERE version_id > 0
        ORDER BY version_id, id DESC
    )
    SELECT count(*) FILTER (WHERE is_applied AND version_id BETWEEN 2 AND 13),
        max(version_id) FILTER (WHERE is_applied)
    INTO applied_versions, latest_version
    FROM latest;
    IF applied_versions <> 12 OR latest_version <> 13
        OR EXISTS (
            SELECT 1 FROM runner_gateway_dispatcher_goose_version
            WHERE version_id > 13
        ) THEN
        RAISE EXCEPTION 'Dispatcher Goose history is incomplete; run the previous image to finish migration 13 before adopting baseline 14';
    END IF;

    -- Checking only names would accept a partial or manually altered schema. In particular,
    -- UID fences, receipts, wake caller identity, and retained Session evidence must survive.
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('dispatch', 'position', 'bigint', true),
            ('dispatch', 'command_id', 'character varying', true),
            ('dispatch', 'session_id', 'character varying', true),
            ('dispatch', 'sandbox_name', 'character varying', true),
            ('dispatch', 'sandbox_uid', 'uuid', true),
            ('dispatch', 'operation', 'character varying', true),
            ('dispatch', 'command', 'bytea', true),
            ('dispatch', 'state', 'character varying', true),
            ('dispatch', 'reason', 'character varying', false),
            ('dispatch', 'admission', 'bytea', false),
            ('dispatch', 'created_at', 'timestamp with time zone', true),
            ('dispatch', 'updated_at', 'timestamp with time zone', true),
            ('dispatch', 'wake_eligible', 'boolean', true),
            ('dispatch', 'wake_from_incarnation', 'character varying', false),
            ('dispatch', 'wake_caller_namespace', 'character varying', false),
            ('dispatch', 'wake_caller_name', 'character varying', false),
            ('dispatch', 'wake_requested_at', 'timestamp with time zone', false),
            ('dispatch', 'traceparent', 'character varying', true),
            ('dispatch', 'actor_account_namespace', 'character varying', false),
            ('dispatch', 'actor_account_name', 'character varying', false),
            ('dispatch', 'actor_account_uid', 'character varying', false),
            ('dispatch', 'actor_pod_name', 'character varying', false),
            ('dispatch', 'actor_pod_uid', 'character varying', false),
            ('dispatch', 'effective_caller_account_namespace', 'character varying', false),
            ('dispatch', 'effective_caller_account_name', 'character varying', false),
            ('dispatch', 'effective_caller_account_uid', 'character varying', false),
            ('dispatch', 'action_request_id', 'uuid', false),
            ('dispatch', 'action_execution_id', 'uuid', false),
            ('dispatch', 'delivery_blocker', 'bytea', false),
            ('dispatch', 'wake_assessment', 'bytea', true),
            ('dispatch', 'delivery_attempted', 'boolean', true),
            ('session', 'session_id', 'character varying', true),
            ('session', 'sandbox_name', 'character varying', true),
            ('session', 'sandbox_uid', 'uuid', true),
            ('session', 'spec', 'bytea', true),
            ('session', 'setup_script', 'character varying', false),
            ('session', 'state', 'character varying', true),
            ('session', 'reason', 'character varying', false),
            ('session', 'created_at', 'timestamp with time zone', true),
            ('session', 'updated_at', 'timestamp with time zone', true),
            ('session', 'wake_eligible', 'boolean', true),
            ('session', 'wake_from_incarnation', 'character varying', false),
            ('session', 'wake_caller_namespace', 'character varying', false),
            ('session', 'wake_caller_name', 'character varying', false),
            ('session', 'wake_requested_at', 'timestamp with time zone', false),
            ('session', 'traceparent', 'character varying', true),
            ('session', 'execution_account_namespace', 'character varying', false),
            ('session', 'execution_account_name', 'character varying', false),
            ('session', 'execution_account_uid', 'character varying', false),
            ('session', 'creator_account_namespace', 'character varying', false),
            ('session', 'creator_account_name', 'character varying', false),
            ('session', 'creator_account_uid', 'character varying', false),
            ('session', 'effective_caller_account_namespace', 'character varying', false),
            ('session', 'effective_caller_account_name', 'character varying', false),
            ('session', 'effective_caller_account_uid', 'character varying', false),
            ('session', 'creator_pod_name', 'character varying', false),
            ('session', 'creator_pod_uid', 'character varying', false),
            ('session', 'action_request_id', 'uuid', false),
            ('session', 'action_execution_id', 'uuid', false),
            ('session', 'delivery_blocker', 'bytea', false),
            ('session', 'wake_assessment', 'bytea', true),
            ('session', 'environment_deleted_at', 'timestamp with time zone', false),
            ('sandbox_ownership', 'sandbox_uid', 'uuid', true),
            ('sandbox_ownership', 'generation', 'bigint', true),
            ('sandbox_ownership', 'lease_uid', 'character varying', true),
            ('sandbox_ownership', 'lease_transitions', 'integer', true),
            ('sandbox_ownership', 'holder_identity', 'character varying', true),
            ('sandbox_ownership', 'updated_at', 'timestamp with time zone', true),
            ('wake_attempt', 'caller_namespace', 'character varying', true),
            ('wake_attempt', 'caller_name', 'character varying', true),
            ('wake_attempt', 'caller_uid', 'character varying', true),
            ('wake_attempt', 'sandbox_uid', 'uuid', true),
            ('wake_attempt', 'wake_generation', 'bigint', true),
            ('wake_attempt', 'attempted_at', 'timestamp with time zone', true),
            ('deleted_sandbox', 'sandbox_uid', 'uuid', true)
        ) AS expected(table_name, column_name, data_type, not_null)
        LEFT JOIN information_schema.columns AS actual
            ON actual.table_schema = current_schema()
            AND actual.table_name = expected.table_name
            AND actual.column_name = expected.column_name
        WHERE actual.column_name IS NULL OR actual.data_type <> expected.data_type
            OR (actual.is_nullable = 'NO') <> expected.not_null
    ) OR EXISTS (
        SELECT 1 FROM (VALUES
            ('dispatch', 'dispatch_pkey'), ('dispatch', 'dispatch_command_id_key'),
            ('dispatch', 'ck_dispatch_actor'), ('dispatch', 'ck_dispatch_actor_pod'),
            ('dispatch', 'ck_dispatch_effective_caller'), ('dispatch', 'ck_dispatch_action_correlation'),
            ('session', 'session_opening_pkey'), ('session', 'session_materialization_state'),
            ('session', 'ck_session_opening_execution_account'),
            ('session', 'ck_session_opening_creator_account'),
            ('session', 'ck_session_opening_effective_caller'),
            ('session', 'ck_session_opening_creator_pod'),
            ('session', 'ck_session_opening_action_correlation'),
            ('sandbox_ownership', 'sandbox_ownership_pkey'),
            ('wake_attempt', 'wake_attempt_pkey'), ('deleted_sandbox', 'deleted_sandbox_pkey')
        ) AS expected(table_name, constraint_name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_constraint AS actual
            WHERE actual.conrelid = to_regclass(format('%I.%I', current_schema(), expected.table_name))
                AND actual.conname = expected.constraint_name
                AND actual.convalidated
        )
    ) OR EXISTS (
        SELECT 1 FROM (VALUES
            ('dispatch', 'ix_dispatch_session_state'),
            ('dispatch', 'ix_dispatch_sandbox_uid_state'),
            ('dispatch', 'ix_dispatch_sandbox_name'),
            ('session', 'ix_session_sandbox_uid'),
            ('wake_attempt', 'ix_wake_attempt_caller_time')
        ) AS expected(table_name, index_name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_index AS actual
            JOIN pg_class AS index_relation ON index_relation.oid = actual.indexrelid
            WHERE actual.indrelid = to_regclass(format('%I.%I', current_schema(), expected.table_name))
                AND index_relation.relname = expected.index_name
                AND actual.indisvalid AND actual.indisready
        )
    ) THEN
        RAISE EXCEPTION 'Dispatcher version-13 schema is incomplete; run the previous image to finish migration 13 or restore the database';
    END IF;
END
$dispatcher_baseline$;
-- +goose StatementEnd

CREATE TABLE IF NOT EXISTS dispatch (
    position BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    command_id VARCHAR NOT NULL UNIQUE,
    session_id VARCHAR NOT NULL,
    sandbox_name VARCHAR NOT NULL,
    sandbox_uid UUID NOT NULL,
    operation VARCHAR NOT NULL,
    command BYTEA NOT NULL,
    state VARCHAR NOT NULL,
    reason VARCHAR,
    admission BYTEA,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    wake_eligible BOOLEAN NOT NULL DEFAULT false,
    wake_from_incarnation VARCHAR,
    wake_caller_namespace VARCHAR,
    wake_caller_name VARCHAR,
    wake_requested_at TIMESTAMPTZ,
    traceparent VARCHAR NOT NULL DEFAULT '',
    actor_account_namespace VARCHAR,
    actor_account_name VARCHAR,
    actor_account_uid VARCHAR,
    actor_pod_name VARCHAR,
    actor_pod_uid VARCHAR,
    effective_caller_account_namespace VARCHAR,
    effective_caller_account_name VARCHAR,
    effective_caller_account_uid VARCHAR,
    action_request_id UUID,
    action_execution_id UUID,
    delivery_blocker BYTEA,
    wake_assessment BYTEA NOT NULL DEFAULT decode('0801', 'hex'),
    delivery_attempted BOOLEAN NOT NULL DEFAULT false,
    CONSTRAINT ck_dispatch_actor CHECK ((actor_account_namespace IS NULL) = (actor_account_name IS NULL)
        AND (actor_account_namespace IS NULL) = (actor_account_uid IS NULL)),
    CONSTRAINT ck_dispatch_actor_pod CHECK ((actor_pod_name IS NULL) = (actor_pod_uid IS NULL)
        AND (actor_pod_name IS NULL OR actor_account_namespace IS NOT NULL)),
    CONSTRAINT ck_dispatch_effective_caller CHECK ((effective_caller_account_namespace IS NULL) = (effective_caller_account_name IS NULL)
        AND (effective_caller_account_namespace IS NULL) = (effective_caller_account_uid IS NULL)),
    CONSTRAINT ck_dispatch_action_correlation CHECK ((action_request_id IS NULL) = (action_execution_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_dispatch_session_state ON dispatch (session_id, state);
CREATE INDEX IF NOT EXISTS ix_dispatch_sandbox_uid_state ON dispatch (sandbox_uid, state);
CREATE INDEX IF NOT EXISTS ix_dispatch_sandbox_name ON dispatch (sandbox_name);

CREATE TABLE IF NOT EXISTS session (
    session_id VARCHAR NOT NULL CONSTRAINT session_opening_pkey PRIMARY KEY,
    sandbox_name VARCHAR NOT NULL,
    sandbox_uid UUID NOT NULL,
    spec BYTEA NOT NULL,
    setup_script VARCHAR,
    state VARCHAR NOT NULL,
    reason VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    wake_eligible BOOLEAN NOT NULL DEFAULT false,
    wake_from_incarnation VARCHAR,
    wake_caller_namespace VARCHAR,
    wake_caller_name VARCHAR,
    wake_requested_at TIMESTAMPTZ,
    traceparent VARCHAR NOT NULL DEFAULT '',
    execution_account_namespace VARCHAR,
    execution_account_name VARCHAR,
    execution_account_uid VARCHAR,
    creator_account_namespace VARCHAR,
    creator_account_name VARCHAR,
    creator_account_uid VARCHAR,
    effective_caller_account_namespace VARCHAR,
    effective_caller_account_name VARCHAR,
    effective_caller_account_uid VARCHAR,
    creator_pod_name VARCHAR,
    creator_pod_uid VARCHAR,
    action_request_id UUID,
    action_execution_id UUID,
    delivery_blocker BYTEA,
    wake_assessment BYTEA NOT NULL DEFAULT decode('0801', 'hex'),
    environment_deleted_at TIMESTAMPTZ,
    CONSTRAINT session_materialization_state CHECK (state IN ('WAITING', 'IN_PROGRESS', 'ACCEPTED', 'FAILED', 'UNKNOWN')),
    CONSTRAINT ck_session_opening_execution_account CHECK ((execution_account_namespace IS NULL) = (execution_account_name IS NULL)
        AND (execution_account_uid IS NULL OR execution_account_namespace IS NOT NULL)),
    CONSTRAINT ck_session_opening_creator_account CHECK ((creator_account_namespace IS NULL) = (creator_account_name IS NULL)
        AND (creator_account_namespace IS NULL) = (creator_account_uid IS NULL)),
    CONSTRAINT ck_session_opening_effective_caller CHECK ((effective_caller_account_namespace IS NULL) = (effective_caller_account_name IS NULL)
        AND (effective_caller_account_namespace IS NULL) = (effective_caller_account_uid IS NULL)),
    CONSTRAINT ck_session_opening_creator_pod CHECK ((creator_pod_name IS NULL) = (creator_pod_uid IS NULL)
        AND (creator_pod_name IS NULL OR creator_account_namespace IS NOT NULL)),
    CONSTRAINT ck_session_opening_action_correlation CHECK ((action_request_id IS NULL) = (action_execution_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_session_sandbox_uid ON session (sandbox_uid);

CREATE TABLE IF NOT EXISTS sandbox_ownership (
    sandbox_uid UUID PRIMARY KEY,
    generation BIGINT NOT NULL,
    lease_uid VARCHAR NOT NULL,
    lease_transitions INTEGER NOT NULL,
    holder_identity VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS wake_attempt (
    caller_namespace VARCHAR NOT NULL,
    caller_name VARCHAR NOT NULL,
    sandbox_uid UUID NOT NULL,
    wake_generation BIGINT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL,
    caller_uid VARCHAR NOT NULL DEFAULT '',
    PRIMARY KEY (caller_namespace, caller_name, caller_uid, sandbox_uid, wake_generation)
);
CREATE INDEX IF NOT EXISTS ix_wake_attempt_caller_time
    ON wake_attempt (caller_namespace, caller_name, caller_uid, attempted_at);

CREATE TABLE IF NOT EXISTS deleted_sandbox (
    sandbox_uid UUID PRIMARY KEY
);
