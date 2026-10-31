-- +goose Up
ALTER TABLE session_opening
    ADD COLUMN execution_account_namespace VARCHAR,
    ADD COLUMN execution_account_name VARCHAR,
    ADD COLUMN execution_account_uid VARCHAR,
    ADD COLUMN creator_account_namespace VARCHAR,
    ADD COLUMN creator_account_name VARCHAR,
    ADD COLUMN creator_account_uid VARCHAR,
    ADD CONSTRAINT ck_session_opening_execution_account
        CHECK ((execution_account_namespace IS NULL) = (execution_account_name IS NULL)
            AND (execution_account_uid IS NULL OR execution_account_namespace IS NOT NULL)),
    ADD CONSTRAINT ck_session_opening_creator_account
        CHECK ((creator_account_namespace IS NULL) = (creator_account_name IS NULL)
            AND (creator_account_namespace IS NULL) = (creator_account_uid IS NULL));

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
