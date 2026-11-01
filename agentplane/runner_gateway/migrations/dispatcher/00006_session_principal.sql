-- +goose Up
ALTER TABLE session_opening
    ADD COLUMN IF NOT EXISTS execution_account_namespace VARCHAR,
    ADD COLUMN IF NOT EXISTS execution_account_name VARCHAR,
    ADD COLUMN IF NOT EXISTS execution_account_uid VARCHAR,
    ADD COLUMN IF NOT EXISTS creator_account_namespace VARCHAR,
    ADD COLUMN IF NOT EXISTS creator_account_name VARCHAR,
    ADD COLUMN IF NOT EXISTS creator_account_uid VARCHAR;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_session_opening_execution_account'
          AND conrelid = 'session_opening'::regclass
    ) THEN
        ALTER TABLE session_opening
            ADD CONSTRAINT ck_session_opening_execution_account
                CHECK ((execution_account_namespace IS NULL) = (execution_account_name IS NULL)
                    AND (execution_account_uid IS NULL OR execution_account_namespace IS NOT NULL));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_session_opening_creator_account'
          AND conrelid = 'session_opening'::regclass
    ) THEN
        ALTER TABLE session_opening
            ADD CONSTRAINT ck_session_opening_creator_account
                CHECK ((creator_account_namespace IS NULL) = (creator_account_name IS NULL)
                    AND (creator_account_namespace IS NULL) = (creator_account_uid IS NULL));
    END IF;
END
$$;

-- +goose Down
ALTER TABLE session_opening
    DROP CONSTRAINT ck_session_opening_creator_account,
    DROP CONSTRAINT ck_session_opening_execution_account,
    DROP COLUMN creator_account_uid,
    DROP COLUMN creator_account_name,
    DROP COLUMN creator_account_namespace,
    DROP COLUMN execution_account_uid,
    DROP COLUMN execution_account_name,
    DROP COLUMN execution_account_namespace;
