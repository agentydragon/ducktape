-- +goose Up
SET LOCAL lock_timeout = '5s';

-- Keep creation JSON until init copies original parameters into immutable typed snapshots.
ALTER TABLE subscription
    ADD COLUMN creation_proto BYTEA,
    ADD COLUMN source_provider VARCHAR;

ALTER TABLE subscription
    ADD CONSTRAINT subscription_creation_storage_pair
        CHECK ((creation_proto IS NULL) = (source_provider IS NULL)) NOT VALID,
    ADD CONSTRAINT subscription_source_provider
        CHECK (source_provider IS NULL OR source_provider IN ('actions', 'github')) NOT VALID,
    ADD CONSTRAINT subscription_typed_source_state
        CHECK (source_provider IS NULL OR CASE source_provider
            WHEN 'actions' THEN actions_after_sequence IS NOT NULL AND github_start_position IS NULL
                AND github_app_id IS NULL AND github_installation_id IS NULL AND github_repository_id IS NULL
                AND github_subject_kind IS NULL AND github_subject_key IS NULL
            WHEN 'github' THEN actions_after_sequence IS NULL AND github_start_position IS NOT NULL
                AND github_app_id IS NOT NULL AND github_installation_id IS NOT NULL
                AND github_repository_id IS NOT NULL AND github_subject_kind IS NOT NULL AND github_subject_key IS NOT NULL
            ELSE false
        END) NOT VALID;

ALTER TABLE notification_storage_backfill_progress
    ADD COLUMN subscription_creation_last_id UUID,
    ADD COLUMN subscription_creation_complete BOOLEAN NOT NULL DEFAULT false;

-- +goose Down
SET LOCAL lock_timeout = '5s';
ALTER TABLE notification_storage_backfill_progress
    DROP COLUMN subscription_creation_complete,
    DROP COLUMN subscription_creation_last_id;
ALTER TABLE subscription
    DROP CONSTRAINT subscription_typed_source_state,
    DROP CONSTRAINT subscription_source_provider,
    DROP CONSTRAINT subscription_creation_storage_pair,
    DROP COLUMN source_provider,
    DROP COLUMN creation_proto;
