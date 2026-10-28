-- +goose Up
SET lock_timeout = '1s';
ALTER TABLE entry
    ADD COLUMN identity_provider VARCHAR,
    ADD COLUMN actions_request_id UUID,
    ADD COLUMN actions_sequence BIGINT,
    ADD COLUMN github_app_id BIGINT,
    ADD COLUMN github_delivery_id UUID,
    ADD COLUMN github_repository_id BIGINT,
    ADD COLUMN github_event VARCHAR,
    ADD COLUMN github_action VARCHAR,
    ADD COLUMN github_pull_request_number BIGINT,
    ADD COLUMN github_mergeability_sequence BIGINT,
    ADD COLUMN github_snapshot_version BIGINT,
    ADD COLUMN message_id UUID,
    ADD COLUMN message_sender_namespace VARCHAR,
    ADD COLUMN message_sender_name VARCHAR,
    ADD CONSTRAINT entry_identity_shape CHECK (
        identity_provider IS NULL OR
        (identity_provider = 'actions'
            AND actions_request_id IS NOT NULL AND actions_sequence IS NOT NULL
            AND github_app_id IS NULL AND github_delivery_id IS NULL AND github_repository_id IS NULL
            AND github_event IS NULL AND github_action IS NULL
            AND github_pull_request_number IS NULL AND github_mergeability_sequence IS NULL
            AND github_snapshot_version IS NULL
            AND message_id IS NULL AND message_sender_namespace IS NULL AND message_sender_name IS NULL)
        OR
        (identity_provider = 'github'
            AND actions_request_id IS NULL AND actions_sequence IS NULL
            AND github_app_id IS NOT NULL AND github_delivery_id IS NOT NULL AND github_repository_id IS NOT NULL
            AND github_event IS NOT NULL
            AND github_pull_request_number IS NULL AND github_mergeability_sequence IS NULL
            AND github_snapshot_version IS NULL
            AND message_id IS NULL AND message_sender_namespace IS NULL AND message_sender_name IS NULL)
        OR
        (identity_provider = 'github_mergeability'
            AND actions_request_id IS NULL AND actions_sequence IS NULL
            AND github_app_id IS NOT NULL AND github_delivery_id IS NULL AND github_repository_id IS NOT NULL
            AND github_event IS NULL AND github_action IS NULL
            AND github_pull_request_number IS NOT NULL
            AND ((github_mergeability_sequence IS NOT NULL AND github_snapshot_version IS NULL)
                OR (github_mergeability_sequence IS NULL AND github_snapshot_version IS NOT NULL))
            AND message_id IS NULL AND message_sender_namespace IS NULL AND message_sender_name IS NULL)
        OR
        (identity_provider = 'message'
            AND actions_request_id IS NULL AND actions_sequence IS NULL
            AND github_app_id IS NULL AND github_delivery_id IS NULL AND github_repository_id IS NULL
            AND github_event IS NULL AND github_action IS NULL
            AND github_pull_request_number IS NULL AND github_mergeability_sequence IS NULL
            AND github_snapshot_version IS NULL
            AND message_id IS NOT NULL
            AND message_sender_namespace IS NOT NULL
            AND message_sender_name IS NOT NULL)
    ) NOT VALID;
RESET lock_timeout;

-- +goose Down
SET lock_timeout = '1s';
ALTER TABLE entry DROP CONSTRAINT entry_identity_shape;
ALTER TABLE entry
    DROP COLUMN identity_provider,
    DROP COLUMN actions_request_id,
    DROP COLUMN actions_sequence,
    DROP COLUMN github_app_id,
    DROP COLUMN github_delivery_id,
    DROP COLUMN github_repository_id,
    DROP COLUMN github_event,
    DROP COLUMN github_action,
    DROP COLUMN github_pull_request_number,
    DROP COLUMN github_mergeability_sequence,
    DROP COLUMN github_snapshot_version,
    DROP COLUMN message_id,
    DROP COLUMN message_sender_namespace,
    DROP COLUMN message_sender_name;
RESET lock_timeout;
