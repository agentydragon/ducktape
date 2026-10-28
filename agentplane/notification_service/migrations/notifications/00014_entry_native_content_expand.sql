-- +goose Up
SET lock_timeout = '1s';
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
    ) NOT VALID,
    ADD CONSTRAINT entry_message_sender_length CHECK (
        (message_sender_namespace IS NULL OR char_length(message_sender_namespace) BETWEEN 1 AND 63) AND
        (message_sender_name IS NULL OR char_length(message_sender_name) BETWEEN 1 AND 253)
    ) NOT VALID;
RESET lock_timeout;

-- +goose Down
SET lock_timeout = '1s';
ALTER TABLE entry
    DROP CONSTRAINT entry_action_content_source,
    DROP CONSTRAINT entry_mergeability_content_source,
    DROP CONSTRAINT entry_message_content_source,
    DROP CONSTRAINT entry_action_content_shape,
    DROP CONSTRAINT entry_mergeability_content_shape,
    DROP CONSTRAINT entry_message_content_shape,
    DROP CONSTRAINT entry_message_body_bytes,
    DROP CONSTRAINT entry_message_from_session_length,
    DROP CONSTRAINT entry_action_actor_length,
    DROP CONSTRAINT entry_message_sender_length,
    DROP COLUMN action_state,
    DROP COLUMN action_at,
    DROP COLUMN action_actor_kind,
    DROP COLUMN action_actor_namespace,
    DROP COLUMN action_actor_name,
    DROP COLUMN action_actor_issuer,
    DROP COLUMN action_actor_subject,
    DROP COLUMN mergeability_repository,
    DROP COLUMN mergeability_base_ref,
    DROP COLUMN mergeability_head_sha,
    DROP COLUMN mergeability_base_sha,
    DROP COLUMN mergeability_previous,
    DROP COLUMN mergeability_current,
    DROP COLUMN mergeability_observed_at,
    DROP COLUMN mergeability_observed_at_submicro_nanos,
    DROP COLUMN merge_commit_sha,
    DROP COLUMN merge_commit_base_sha,
    DROP COLUMN merge_commit_head_sha,
    DROP COLUMN message_body,
    DROP COLUMN message_from_session;
RESET lock_timeout;
