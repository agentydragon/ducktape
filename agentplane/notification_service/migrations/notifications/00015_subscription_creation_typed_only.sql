-- +goose Up
SET LOCAL lock_timeout = '5s';

-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM subscription LIMIT 1) AND NOT EXISTS (
        SELECT 1 FROM notification_storage_backfill_progress
        WHERE singleton AND subscription_creation_complete
    ) THEN
        RAISE EXCEPTION 'subscription creation backfill must complete before the typed creation contract';
    END IF;
END $$;
-- +goose StatementEnd

-- Replace the original JSON checks atomically with checks over the typed source discriminator.
ALTER TABLE subscription
    DROP CONSTRAINT subscription_creation_source,
    DROP CONSTRAINT subscription_source_state,
    ADD CONSTRAINT subscription_creation_typed_required
        CHECK (creation_proto IS NOT NULL AND source_provider IS NOT NULL) NOT VALID;

-- The final writer uses the typed columns, so the old JSON column becomes nullable before contract.
ALTER TABLE subscription ALTER COLUMN creation DROP NOT NULL;

-- +goose Down
SET LOCAL lock_timeout = '5s';

-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM subscription WHERE creation IS NULL) THEN
        RAISE EXCEPTION 'subscription creation JSON has NULL rows; retain the typed contract';
    END IF;
END $$;
-- +goose StatementEnd

ALTER TABLE subscription ALTER COLUMN creation SET NOT NULL;
ALTER TABLE subscription
    DROP CONSTRAINT subscription_creation_typed_required,
    ADD CONSTRAINT subscription_creation_source CHECK (creation ? 'source') NOT VALID,
    ADD CONSTRAINT subscription_source_state CHECK (
        CASE creation #>> '{source,provider}'
            WHEN 'actions' THEN actions_after_sequence IS NOT NULL AND github_start_position IS NULL
                AND github_app_id IS NULL AND github_installation_id IS NULL AND github_repository_id IS NULL
                AND github_subject_kind IS NULL AND github_subject_key IS NULL
            WHEN 'github' THEN actions_after_sequence IS NULL AND github_start_position IS NOT NULL
                AND github_app_id IS NOT NULL AND github_installation_id IS NOT NULL
                AND github_repository_id IS NOT NULL
                AND github_subject_kind IS NOT NULL AND github_subject_key IS NOT NULL
            ELSE false
        END) NOT VALID;
