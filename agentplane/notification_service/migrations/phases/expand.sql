SET LOCAL lock_timeout = '5s';
-- Nullable fields admit old rows until init backfills their relational destination keys.
ALTER TABLE inbox
    ADD COLUMN destination_namespace VARCHAR,
    ADD COLUMN destination_name VARCHAR,
    ADD COLUMN destination_uid VARCHAR;

CREATE TABLE notification_storage_backfill_progress (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    inbox_destination_last_id UUID,
    inbox_destination_complete BOOLEAN NOT NULL DEFAULT false
);
INSERT INTO notification_storage_backfill_progress (singleton) VALUES (true);

SET LOCAL lock_timeout = '1s';
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
    ADD COLUMN github_observation_sequence BIGINT,
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
            AND github_snapshot_version IS NULL AND github_observation_sequence IS NULL
            AND message_id IS NULL AND message_sender_namespace IS NULL AND message_sender_name IS NULL)
        OR
        (identity_provider = 'github'
            AND actions_request_id IS NULL AND actions_sequence IS NULL
            AND github_app_id IS NOT NULL AND github_delivery_id IS NOT NULL AND github_repository_id IS NOT NULL
            AND github_event IS NOT NULL
            AND github_observation_sequence IS NULL
            AND github_pull_request_number IS NULL AND github_mergeability_sequence IS NULL
            AND github_snapshot_version IS NULL
            AND message_id IS NULL AND message_sender_namespace IS NULL AND message_sender_name IS NULL)
        OR
        (identity_provider = 'github_observation'
            AND actions_request_id IS NULL AND actions_sequence IS NULL
            AND github_app_id IS NOT NULL AND github_delivery_id IS NULL AND github_repository_id IS NOT NULL
            AND github_event IS NOT NULL AND github_action IS NULL
            AND github_observation_sequence IS NOT NULL
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
            AND github_observation_sequence IS NULL
            AND message_id IS NULL AND message_sender_namespace IS NULL AND message_sender_name IS NULL)
        OR
        (identity_provider = 'message'
            AND actions_request_id IS NULL AND actions_sequence IS NULL
            AND github_app_id IS NULL AND github_delivery_id IS NULL AND github_repository_id IS NULL
            AND github_event IS NULL AND github_action IS NULL
            AND github_pull_request_number IS NULL AND github_mergeability_sequence IS NULL
            AND github_snapshot_version IS NULL AND github_observation_sequence IS NULL
            AND message_id IS NOT NULL
            AND message_sender_namespace IS NOT NULL
            AND message_sender_name IS NOT NULL)
    ) NOT VALID;

-- Init records bounded backfill progress here and builds natural-key indexes concurrently.
SET LOCAL lock_timeout = '1s';
CREATE TABLE notification_event_identity_backfill (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'backfilling'
        CHECK (phase IN ('backfilling', 'backfilled', 'indexed')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);

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

SET LOCAL lock_timeout = '1s';
ALTER TABLE entry
    ADD COLUMN action_state INTEGER,
    ADD COLUMN action_at TIMESTAMPTZ,
    ADD COLUMN action_actor_kind VARCHAR,
    ADD COLUMN action_actor_namespace VARCHAR,
    ADD COLUMN action_actor_name VARCHAR,
    ADD COLUMN action_actor_issuer VARCHAR,
    ADD COLUMN action_actor_subject VARCHAR,
    ADD COLUMN mergeability_repository VARCHAR,
    ADD COLUMN mergeability_base_ref VARCHAR,
    ADD COLUMN mergeability_head_sha VARCHAR,
    ADD COLUMN mergeability_base_sha VARCHAR,
    ADD COLUMN mergeability_previous VARCHAR,
    ADD COLUMN mergeability_current VARCHAR,
    ADD COLUMN mergeability_observed_at TIMESTAMPTZ,
    ADD COLUMN mergeability_observed_at_submicro_nanos SMALLINT,
    ADD COLUMN merge_commit_sha VARCHAR,
    ADD COLUMN merge_commit_base_sha VARCHAR,
    ADD COLUMN merge_commit_head_sha VARCHAR,
    ADD COLUMN message_body TEXT,
    ADD COLUMN message_from_session TEXT,
    ADD CONSTRAINT entry_action_content_source CHECK (action_at IS NULL OR (identity_provider IS NOT NULL AND identity_provider = 'actions')) NOT VALID,
    ADD CONSTRAINT entry_mergeability_content_source CHECK (mergeability_observed_at IS NULL OR (identity_provider IS NOT NULL AND identity_provider = 'github_mergeability')) NOT VALID,
    ADD CONSTRAINT entry_message_content_source CHECK (message_body IS NULL OR (identity_provider IS NOT NULL AND identity_provider = 'message')) NOT VALID,
    ADD CONSTRAINT entry_action_content_shape CHECK (
        (action_at IS NULL AND action_state IS NULL AND action_actor_kind IS NULL AND
         action_actor_namespace IS NULL AND action_actor_name IS NULL AND action_actor_issuer IS NULL AND action_actor_subject IS NULL)
        OR (action_at IS NOT NULL AND action_state IS NOT NULL AND (
            (action_actor_kind IS NULL AND action_actor_namespace IS NULL AND action_actor_name IS NULL AND action_actor_issuer IS NULL AND action_actor_subject IS NULL)
            OR (action_actor_kind IS NOT NULL AND action_actor_kind = 'caller' AND action_actor_namespace IS NOT NULL AND action_actor_name IS NOT NULL AND action_actor_issuer IS NULL AND action_actor_subject IS NULL)
            OR (action_actor_kind IS NOT NULL AND action_actor_kind = 'operator' AND action_actor_namespace IS NULL AND action_actor_name IS NULL AND action_actor_issuer IS NOT NULL AND action_actor_subject IS NOT NULL)
        ))
    ) NOT VALID,
    ADD CONSTRAINT entry_mergeability_content_shape CHECK (
        (mergeability_observed_at IS NULL AND mergeability_observed_at_submicro_nanos IS NULL AND mergeability_repository IS NULL AND mergeability_base_ref IS NULL AND
         mergeability_head_sha IS NULL AND mergeability_base_sha IS NULL AND mergeability_previous IS NULL AND
         mergeability_current IS NULL AND merge_commit_sha IS NULL AND merge_commit_base_sha IS NULL AND merge_commit_head_sha IS NULL)
        OR (mergeability_observed_at IS NOT NULL AND mergeability_observed_at_submicro_nanos IS NOT NULL AND mergeability_observed_at_submicro_nanos BETWEEN 0 AND 999 AND mergeability_repository IS NOT NULL AND mergeability_base_ref IS NOT NULL AND
            mergeability_head_sha IS NOT NULL AND mergeability_base_sha IS NOT NULL AND
            mergeability_previous IS NOT NULL AND mergeability_previous IN ('UNKNOWN', 'MERGEABLE', 'CONFLICTING') AND
            mergeability_current IS NOT NULL AND mergeability_current IN ('UNKNOWN', 'MERGEABLE', 'CONFLICTING') AND
            ((merge_commit_sha IS NULL AND merge_commit_base_sha IS NULL AND merge_commit_head_sha IS NULL) OR
             (merge_commit_sha IS NOT NULL AND merge_commit_base_sha IS NOT NULL AND merge_commit_head_sha IS NOT NULL)))
    ) NOT VALID,
    ADD CONSTRAINT entry_message_content_shape CHECK (message_body IS NOT NULL OR message_from_session IS NULL) NOT VALID,
    ADD CONSTRAINT entry_message_body_bytes CHECK (message_body IS NULL OR octet_length(message_body) BETWEEN 1 AND 262144) NOT VALID,
    ADD CONSTRAINT entry_message_from_session_length CHECK (message_from_session IS NULL OR char_length(message_from_session) BETWEEN 1 AND 200) NOT VALID,
    ADD CONSTRAINT entry_action_actor_length CHECK (
        (action_actor_namespace IS NULL OR char_length(action_actor_namespace) BETWEEN 1 AND 63) AND
        (action_actor_name IS NULL OR char_length(action_actor_name) BETWEEN 1 AND 253) AND
        (action_actor_issuer IS NULL OR char_length(action_actor_issuer) > 0) AND
        (action_actor_subject IS NULL OR char_length(action_actor_subject) > 0)
    ) NOT VALID;

-- Init uses this durable checkpoint for bounded native-content backfill.
SET LOCAL lock_timeout = '1s';
CREATE TABLE notification_entry_content_backfill (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_inbox_id UUID,
    last_cursor BIGINT,
    phase VARCHAR NOT NULL DEFAULT 'backfilling'
        CHECK (phase IN ('backfilling', 'scanned', 'backfilled')),
    CHECK ((last_inbox_id IS NULL) = (last_cursor IS NULL))
);

-- This row is the durable bridge between the final DDL commit and Goose recording version 9.
CREATE TABLE notification_storage_cutover (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    phase VARCHAR NOT NULL CHECK (phase IN ('expanded', 'fenced', 'complete'))
);
INSERT INTO notification_storage_cutover (singleton, phase) VALUES (true, 'expanded');
