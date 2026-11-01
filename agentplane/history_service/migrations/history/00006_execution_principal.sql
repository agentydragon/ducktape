-- +goose Up
-- Keep prior rows explicitly unassociated. There is no safe way to infer a retained execution principal from a
-- writer, creator, Sandbox name or current ServiceAccount owner.
ALTER TABLE session_history
    ADD COLUMN IF NOT EXISTS execution_principal_namespace VARCHAR,
    ADD COLUMN IF NOT EXISTS execution_principal_name VARCHAR,
    ADD COLUMN IF NOT EXISTS execution_principal_uid VARCHAR;

-- +goose StatementBegin
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'history_execution_principal_complete'
          AND conrelid = 'session_history'::regclass
    ) THEN
        ALTER TABLE session_history
            ADD CONSTRAINT history_execution_principal_complete
            CHECK (num_nulls(execution_principal_namespace, execution_principal_name, execution_principal_uid) IN (0, 3));
    END IF;
END
$$;
-- +goose StatementEnd

CREATE INDEX IF NOT EXISTS ix_history_execution_principal_opened
    ON session_history (execution_principal_namespace, execution_principal_name, execution_principal_uid, opened_position);

-- +goose Down
-- The principal association cannot be reconstructed after it is dropped. Keep the additive schema
-- across an application rollback; Up is idempotent so a later deployment can reapply this migration.
SELECT 1;
