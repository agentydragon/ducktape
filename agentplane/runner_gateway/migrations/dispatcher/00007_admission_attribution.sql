-- +goose Up
ALTER TABLE session_opening
    ADD COLUMN IF NOT EXISTS effective_caller_account_namespace VARCHAR,
    ADD COLUMN IF NOT EXISTS effective_caller_account_name VARCHAR,
    ADD COLUMN IF NOT EXISTS effective_caller_account_uid VARCHAR,
    ADD COLUMN IF NOT EXISTS creator_pod_name VARCHAR,
    ADD COLUMN IF NOT EXISTS creator_pod_uid VARCHAR,
    ADD COLUMN IF NOT EXISTS action_request_id UUID,
    ADD COLUMN IF NOT EXISTS action_execution_id UUID;

ALTER TABLE dispatch
    ADD COLUMN IF NOT EXISTS actor_account_namespace VARCHAR,
    ADD COLUMN IF NOT EXISTS actor_account_name VARCHAR,
    ADD COLUMN IF NOT EXISTS actor_account_uid VARCHAR,
    ADD COLUMN IF NOT EXISTS actor_pod_name VARCHAR,
    ADD COLUMN IF NOT EXISTS actor_pod_uid VARCHAR,
    ADD COLUMN IF NOT EXISTS effective_caller_account_namespace VARCHAR,
    ADD COLUMN IF NOT EXISTS effective_caller_account_name VARCHAR,
    ADD COLUMN IF NOT EXISTS effective_caller_account_uid VARCHAR,
    ADD COLUMN IF NOT EXISTS action_request_id UUID,
    ADD COLUMN IF NOT EXISTS action_execution_id UUID;

-- +goose StatementBegin
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_session_opening_effective_caller' AND conrelid = 'session_opening'::regclass) THEN
        ALTER TABLE session_opening ADD CONSTRAINT ck_session_opening_effective_caller
            CHECK ((effective_caller_account_namespace IS NULL) = (effective_caller_account_name IS NULL)
                AND (effective_caller_account_namespace IS NULL) = (effective_caller_account_uid IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_session_opening_creator_pod' AND conrelid = 'session_opening'::regclass) THEN
        ALTER TABLE session_opening ADD CONSTRAINT ck_session_opening_creator_pod
            CHECK ((creator_pod_name IS NULL) = (creator_pod_uid IS NULL)
                AND (creator_pod_name IS NULL OR creator_account_namespace IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_session_opening_action_correlation' AND conrelid = 'session_opening'::regclass) THEN
        ALTER TABLE session_opening ADD CONSTRAINT ck_session_opening_action_correlation
            CHECK ((action_request_id IS NULL) = (action_execution_id IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_dispatch_actor' AND conrelid = 'dispatch'::regclass) THEN
        ALTER TABLE dispatch ADD CONSTRAINT ck_dispatch_actor
            CHECK ((actor_account_namespace IS NULL) = (actor_account_name IS NULL)
                AND (actor_account_namespace IS NULL) = (actor_account_uid IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_dispatch_actor_pod' AND conrelid = 'dispatch'::regclass) THEN
        ALTER TABLE dispatch ADD CONSTRAINT ck_dispatch_actor_pod
            CHECK ((actor_pod_name IS NULL) = (actor_pod_uid IS NULL)
                AND (actor_pod_name IS NULL OR actor_account_namespace IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_dispatch_effective_caller' AND conrelid = 'dispatch'::regclass) THEN
        ALTER TABLE dispatch ADD CONSTRAINT ck_dispatch_effective_caller
            CHECK ((effective_caller_account_namespace IS NULL) = (effective_caller_account_name IS NULL)
                AND (effective_caller_account_namespace IS NULL) = (effective_caller_account_uid IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_dispatch_action_correlation' AND conrelid = 'dispatch'::regclass) THEN
        ALTER TABLE dispatch ADD CONSTRAINT ck_dispatch_action_correlation
            CHECK ((action_request_id IS NULL) = (action_execution_id IS NULL));
    END IF;
END $$;
-- +goose StatementEnd

-- +goose Down
ALTER TABLE dispatch
    DROP CONSTRAINT ck_dispatch_action_correlation,
    DROP CONSTRAINT ck_dispatch_effective_caller,
    DROP CONSTRAINT ck_dispatch_actor_pod,
    DROP CONSTRAINT ck_dispatch_actor,
    DROP COLUMN action_execution_id,
    DROP COLUMN action_request_id,
    DROP COLUMN effective_caller_account_uid,
    DROP COLUMN effective_caller_account_name,
    DROP COLUMN effective_caller_account_namespace,
    DROP COLUMN actor_pod_uid,
    DROP COLUMN actor_pod_name,
    DROP COLUMN actor_account_uid,
    DROP COLUMN actor_account_name,
    DROP COLUMN actor_account_namespace;

ALTER TABLE session_opening
    DROP CONSTRAINT ck_session_opening_action_correlation,
    DROP CONSTRAINT ck_session_opening_creator_pod,
    DROP CONSTRAINT ck_session_opening_effective_caller,
    DROP COLUMN action_execution_id,
    DROP COLUMN action_request_id,
    DROP COLUMN creator_pod_uid,
    DROP COLUMN creator_pod_name,
    DROP COLUMN effective_caller_account_uid,
    DROP COLUMN effective_caller_account_name,
    DROP COLUMN effective_caller_account_namespace;
