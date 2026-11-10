-- +goose Up
-- Fresh databases receive the current schema. Existing databases are adopted only after
-- completed Goose version 12 and the required schema objects validate.
-- +goose StatementBegin
DO $notification_baseline$
DECLARE
    current_version BIGINT;
    applied_versions INTEGER;
    earlier_versions INTEGER;
    earlier_history INTEGER;
    highest_recorded_version BIGINT;
    expected_tables text[] := ARRAY[
        'entry', 'github_commit_current', 'github_commit_event', 'github_commit_refresh',
        'github_commit_subscription_snapshot', 'github_delivery', 'github_delivery_subject',
        'github_installation', 'github_mergeability_event', 'github_pr_state_current',
        'github_pr_state_event', 'github_pr_state_subscription_snapshot', 'github_pull_request',
        'github_repository', 'github_repository_access', 'github_review_refresh',
        'github_subject', 'github_subject_revision', 'inbox', 'message', 'notice',
        'subscription', 'subscription_match'
    ];
    expected_table text;
BEGIN
    IF to_regclass(format('%I.%I', current_schema(), 'notification_service_goose_version')) IS NULL THEN
        RAISE EXCEPTION 'Notification Goose history is missing; run the previous image to finish migration 12';
    END IF;
    WITH latest AS (
        SELECT DISTINCT ON (version_id) version_id, is_applied
        FROM notification_service_goose_version
        ORDER BY version_id, id DESC
    )
    SELECT COALESCE(max(version_id) FILTER (WHERE is_applied), 0),
           count(*) FILTER (WHERE version_id BETWEEN 10 AND 12 AND is_applied),
           count(*) FILTER (WHERE version_id BETWEEN 1 AND 9 AND is_applied),
           count(*) FILTER (WHERE version_id BETWEEN 1 AND 9),
           COALESCE(max(version_id), 0)
    INTO current_version, applied_versions, earlier_versions, earlier_history, highest_recorded_version FROM latest;
    IF current_version = 12 AND applied_versions = 3 AND earlier_versions = earlier_history
       AND earlier_versions IN (0, 9) AND highest_recorded_version = 12 THEN
        FOREACH expected_table IN ARRAY expected_tables LOOP
            IF to_regclass(format('%I.%I', current_schema(), expected_table)) IS NULL THEN
                RAISE EXCEPTION 'Notification version-12 schema is incomplete (missing %); run the previous image to finish migration 12', expected_table;
            END IF;
        END LOOP;
        IF EXISTS (
            SELECT 1 FROM (VALUES
                ('inbox', 'session_id'), ('inbox', 'destination_uid'),
                ('subscription', 'creation_proto'), ('entry', 'identity_provider'),
                ('entry', 'message_body'), ('notice', 'runner_entry')
            ) AS expected(table_name, column_name)
            WHERE NOT EXISTS (
                SELECT 1 FROM information_schema.columns actual
                WHERE actual.table_schema = current_schema() AND actual.table_name = expected.table_name
                    AND actual.column_name = expected.column_name
            )
        ) OR EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND
                ((table_name = 'subscription' AND column_name = 'github_ci_after_sequence') OR
                 (table_name = 'github_delivery' AND column_name = 'ci_normalized'))
        ) THEN
            RAISE EXCEPTION 'Notification version-12 schema columns are incomplete; run the previous image to finish migration 12';
        END IF;
        IF EXISTS (SELECT 1 FROM unnest(ARRAY['github_ci_current', 'github_ci_event', 'github_ci_refresh', 'github_ci_subscription_snapshot']) retired(name)
                   WHERE to_regclass(format('%I.%I', current_schema(), retired.name)) IS NOT NULL)
           OR to_regclass(format('%I.%I', current_schema(), 'notification_storage_cutover')) IS NOT NULL
        THEN
            RAISE EXCEPTION 'Notification version-12 schema has unfinished legacy objects; run the previous image to finish migration 12';
        END IF;
        IF (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE i.indrelid = to_regclass(format('%I.%I', current_schema(), 'inbox'))
              AND c.relname = 'inbox_session_id_unique' AND i.indisunique AND i.indisvalid AND i.indisready) <> 1
           OR (SELECT count(*) FROM pg_trigger t WHERE t.tgrelid = to_regclass(format('%I.%I', current_schema(), 'inbox'))
               AND t.tgname = 'inbox_watch_change' AND NOT t.tgisinternal AND t.tgenabled <> 'D') <> 1
        THEN
            RAISE EXCEPTION 'Notification version-12 Session inbox schema is incomplete; run the previous image to finish migration 12';
        END IF;
        RETURN;
    END IF;
    IF current_version <> 0 OR applied_versions <> 0 OR earlier_history <> 0 OR highest_recorded_version <> 0 THEN
        RAISE EXCEPTION 'Notification Goose history is at version %, expected 12; run the previous image to finish migration 12', current_version;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class relation JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = current_schema() AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND relation.relname NOT IN ('notification_service_goose_version', 'notification_service_goose_version_id_seq')
    ) THEN
        RAISE EXCEPTION 'Notification database has unrecognized objects; run the previous image to finish migration 12';
    END IF;
CREATE TABLE entry (
    inbox_id uuid NOT NULL,
    cursor bigint NOT NULL,
    payload jsonb,
    created_at timestamp with time zone NOT NULL,
    identity_provider character varying NOT NULL,
    actions_request_id uuid,
    actions_sequence bigint,
    github_app_id bigint,
    github_delivery_id uuid,
    github_repository_id bigint,
    github_event character varying,
    github_action character varying,
    github_pull_request_number bigint,
    github_mergeability_sequence bigint,
    github_snapshot_version bigint,
    github_observation_sequence bigint,
    message_id uuid,
    message_sender_namespace character varying,
    message_sender_name character varying,
    action_state integer,
    action_at timestamp with time zone,
    action_actor_kind character varying,
    action_actor_namespace character varying,
    action_actor_name character varying,
    action_actor_issuer character varying,
    action_actor_subject character varying,
    mergeability_repository character varying,
    mergeability_base_ref character varying,
    mergeability_head_sha character varying,
    mergeability_base_sha character varying,
    mergeability_previous character varying,
    mergeability_current character varying,
    mergeability_observed_at timestamp with time zone,
    mergeability_observed_at_submicro_nanos smallint,
    merge_commit_sha character varying,
    merge_commit_base_sha character varying,
    merge_commit_head_sha character varying,
    message_body text,
    message_from_session text,
    CONSTRAINT entry_action_actor_length CHECK (
        (action_actor_namespace IS NULL OR char_length(action_actor_namespace) BETWEEN 1 AND 63) AND
        (action_actor_name IS NULL OR char_length(action_actor_name) BETWEEN 1 AND 253) AND
        (action_actor_issuer IS NULL OR char_length(action_actor_issuer) > 0) AND
        (action_actor_subject IS NULL OR char_length(action_actor_subject) > 0)
    ),
    CONSTRAINT entry_action_content_shape CHECK (
        (action_at IS NULL AND action_state IS NULL AND action_actor_kind IS NULL AND
         action_actor_namespace IS NULL AND action_actor_name IS NULL AND action_actor_issuer IS NULL AND action_actor_subject IS NULL)
        OR (action_at IS NOT NULL AND action_state IS NOT NULL AND (
            (action_actor_kind IS NULL AND action_actor_namespace IS NULL AND action_actor_name IS NULL AND action_actor_issuer IS NULL AND action_actor_subject IS NULL)
            OR (action_actor_kind IS NOT NULL AND action_actor_kind = 'caller' AND action_actor_namespace IS NOT NULL AND action_actor_name IS NOT NULL AND action_actor_issuer IS NULL AND action_actor_subject IS NULL)
            OR (action_actor_kind IS NOT NULL AND action_actor_kind = 'operator' AND action_actor_namespace IS NULL AND action_actor_name IS NULL AND action_actor_issuer IS NOT NULL AND action_actor_subject IS NOT NULL)
        ))
    ),
    CONSTRAINT entry_action_content_source CHECK (
        action_at IS NULL OR (identity_provider IS NOT NULL AND identity_provider = 'actions')
    ),
    CONSTRAINT entry_identity_shape CHECK (
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
    ),
    CONSTRAINT entry_mergeability_content_shape CHECK (
        (mergeability_observed_at IS NULL AND mergeability_observed_at_submicro_nanos IS NULL AND mergeability_repository IS NULL AND mergeability_base_ref IS NULL AND
         mergeability_head_sha IS NULL AND mergeability_base_sha IS NULL AND mergeability_previous IS NULL AND
         mergeability_current IS NULL AND merge_commit_sha IS NULL AND merge_commit_base_sha IS NULL AND merge_commit_head_sha IS NULL)
        OR (mergeability_observed_at IS NOT NULL AND mergeability_observed_at_submicro_nanos IS NOT NULL AND mergeability_observed_at_submicro_nanos BETWEEN 0 AND 999 AND mergeability_repository IS NOT NULL AND mergeability_base_ref IS NOT NULL AND
            mergeability_head_sha IS NOT NULL AND mergeability_base_sha IS NOT NULL AND
            mergeability_previous IS NOT NULL AND (mergeability_previous)::text = ANY (
                (ARRAY['UNKNOWN'::character varying, 'MERGEABLE'::character varying, 'CONFLICTING'::character varying])::text[]
            ) AND
            mergeability_current IS NOT NULL AND (mergeability_current)::text = ANY (
                (ARRAY['UNKNOWN'::character varying, 'MERGEABLE'::character varying, 'CONFLICTING'::character varying])::text[]
            ) AND
            ((merge_commit_sha IS NULL AND merge_commit_base_sha IS NULL AND merge_commit_head_sha IS NULL) OR
             (merge_commit_sha IS NOT NULL AND merge_commit_base_sha IS NOT NULL AND merge_commit_head_sha IS NOT NULL)))
    ),
    CONSTRAINT entry_mergeability_content_source CHECK (
        mergeability_observed_at IS NULL OR
        (identity_provider IS NOT NULL AND identity_provider = 'github_mergeability')
    ),
    CONSTRAINT entry_message_body_bytes CHECK (
        message_body IS NULL OR octet_length(message_body) BETWEEN 1 AND 262144
    ),
    CONSTRAINT entry_message_content_shape CHECK (
        message_body IS NOT NULL OR message_from_session IS NULL
    ),
    CONSTRAINT entry_message_content_source CHECK (
        message_body IS NULL OR (identity_provider IS NOT NULL AND identity_provider = 'message')
    ),
    CONSTRAINT entry_message_from_session_length CHECK (
        message_from_session IS NULL OR char_length(message_from_session) BETWEEN 1 AND 200
    ),
    CONSTRAINT entry_owned_legacy_content_absent CHECK (
        identity_provider IS NULL OR (identity_provider)::text <> ALL (
            (ARRAY['actions'::character varying, 'github_mergeability'::character varying, 'message'::character varying])::text[]
        ) OR payload IS NULL
    )
);

CREATE TABLE github_commit_current (
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    head_sha character varying NOT NULL,
    event character varying NOT NULL,
    entity_key character varying NOT NULL,
    last_sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT github_commit_current_event_check CHECK (((event)::text = ANY ((ARRAY['check_run'::character varying, 'status'::character varying])::text[])))
);

CREATE TABLE github_commit_event (
    sequence bigint NOT NULL,
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    head_sha character varying NOT NULL,
    event character varying NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT github_commit_event_event_check CHECK (((event)::text = ANY ((ARRAY['check_run'::character varying, 'status'::character varying])::text[])))
);

CREATE TABLE github_commit_refresh (
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    subject_kind character varying NOT NULL,
    subject_key character varying NOT NULL,
    event character varying NOT NULL,
    generation bigint NOT NULL,
    last_success_at timestamp with time zone,
    error_kind character varying,
    error character varying,
    error_since timestamp with time zone,
    error_observed_at timestamp with time zone,
    next_attempt timestamp with time zone,
    claim uuid,
    claim_until timestamp with time zone,
    CONSTRAINT github_commit_refresh_error_kind CHECK (((error_kind)::text = ANY ((ARRAY['rate_limited'::character varying, 'unavailable'::character varying, 'access_denied'::character varying, 'source_changed'::character varying, 'processing_error'::character varying])::text[]))),
    CONSTRAINT github_commit_refresh_error_state CHECK ((((error IS NULL) AND (error_kind IS NULL) AND (error_since IS NULL) AND (error_observed_at IS NULL)) OR ((error IS NOT NULL) AND (error_kind IS NOT NULL) AND (error_since IS NOT NULL) AND (error_observed_at IS NOT NULL)))),
    CONSTRAINT github_commit_refresh_event_check CHECK (((event)::text = ANY ((ARRAY['check_run'::character varying, 'status'::character varying])::text[]))),
    CONSTRAINT github_commit_refresh_subject_kind_check CHECK (((subject_kind)::text = 'pull_request'::text))
);

CREATE TABLE github_commit_subscription_snapshot (
    subscription_id uuid NOT NULL,
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    event character varying NOT NULL,
    sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT github_commit_subscription_snapshot_event_check CHECK (((event)::text = ANY ((ARRAY['check_run'::character varying, 'status'::character varying])::text[])))
);

CREATE TABLE github_delivery (
    "position" bigint NOT NULL,
    app_id bigint NOT NULL,
    delivery_id uuid NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint,
    event character varying NOT NULL,
    action character varying,
    head_sha character varying,
    digest bytea NOT NULL,
    payload jsonb NOT NULL,
    received_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    commit_normalized boolean DEFAULT false NOT NULL,
    pr_normalized boolean DEFAULT false NOT NULL,
    pr_normalized_action character varying
);

CREATE TABLE github_delivery_subject (
    delivery_position bigint NOT NULL,
    repository_id bigint NOT NULL,
    kind character varying NOT NULL,
    subject_key character varying NOT NULL
);

CREATE TABLE github_installation (
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    generation bigint NOT NULL
);

CREATE TABLE github_mergeability_event (
    sequence bigint NOT NULL,
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    pull_request_number bigint NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);

CREATE TABLE github_pr_state_current (
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    pull_request_number bigint NOT NULL,
    event character varying NOT NULL,
    entity_id bigint NOT NULL,
    last_sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT github_pr_state_current_event_check CHECK (((event)::text = ANY ((ARRAY['pull_request'::character varying, 'pull_request_review'::character varying])::text[])))
);

CREATE TABLE github_pr_state_event (
    sequence bigint NOT NULL,
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    pull_request_number bigint NOT NULL,
    event character varying NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT github_pr_state_event_event_check CHECK (((event)::text = ANY ((ARRAY['pull_request'::character varying, 'pull_request_review'::character varying])::text[])))
);

CREATE TABLE github_pr_state_subscription_snapshot (
    subscription_id uuid NOT NULL,
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    pull_request_number bigint CONSTRAINT github_pr_state_subscription_snaps_pull_request_number_not_null NOT NULL,
    event character varying NOT NULL,
    sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT github_pr_state_subscription_snapshot_event_check CHECK (((event)::text = ANY ((ARRAY['pull_request'::character varying, 'pull_request_review'::character varying])::text[])))
);

CREATE TABLE github_pull_request (
    repository_id bigint NOT NULL,
    number bigint NOT NULL,
    base_ref character varying NOT NULL,
    head_sha character varying NOT NULL,
    base_sha character varying NOT NULL,
    state character varying NOT NULL,
    mergeability character varying NOT NULL,
    last_settled_mergeability character varying NOT NULL,
    snapshot_version bigint DEFAULT 1 NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    merge_commit_sha character varying,
    mergeability_recheck_attempt integer DEFAULT 0 NOT NULL,
    CONSTRAINT github_pull_request_last_settled_mergeability_check CHECK (((last_settled_mergeability)::text = ANY ((ARRAY['MERGEABLE'::character varying, 'CONFLICTING'::character varying, 'UNKNOWN'::character varying])::text[]))),
    CONSTRAINT github_pull_request_mergeability_check CHECK (((mergeability)::text = ANY ((ARRAY['MERGEABLE'::character varying, 'CONFLICTING'::character varying, 'UNKNOWN'::character varying])::text[]))),
    CONSTRAINT github_pull_request_mergeability_recheck_attempt_check CHECK ((mergeability_recheck_attempt >= 0)),
    CONSTRAINT github_pull_request_number_check CHECK ((number > 0)),
    CONSTRAINT github_pull_request_state_check CHECK (((state)::text = ANY ((ARRAY['OPEN'::character varying, 'CLOSED'::character varying, 'MERGED'::character varying])::text[])))
);

CREATE TABLE github_repository (
    repository_id bigint NOT NULL,
    full_name character varying NOT NULL
);

CREATE TABLE github_repository_access (
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    generation bigint NOT NULL,
    validated_installation_generation bigint,
    checked_at timestamp with time zone,
    valid_until timestamp with time zone,
    last_success_at timestamp with time zone,
    error_kind character varying,
    error character varying,
    error_since timestamp with time zone,
    error_observed_at timestamp with time zone,
    next_attempt timestamp with time zone,
    claim uuid,
    claim_until timestamp with time zone,
    CONSTRAINT github_access_error_kind CHECK (((error_kind)::text = ANY ((ARRAY['rate_limited'::character varying, 'unavailable'::character varying, 'access_denied'::character varying, 'source_changed'::character varying, 'processing_error'::character varying])::text[]))),
    CONSTRAINT github_access_error_state CHECK ((((error IS NULL) AND (error_kind IS NULL) AND (error_since IS NULL) AND (error_observed_at IS NULL)) OR ((error IS NOT NULL) AND (error_kind IS NOT NULL) AND (error_since IS NOT NULL) AND (error_observed_at IS NOT NULL))))
);

CREATE TABLE github_review_refresh (
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    subject_kind character varying NOT NULL,
    subject_key character varying NOT NULL,
    generation bigint NOT NULL,
    last_success_at timestamp with time zone,
    error_kind character varying,
    error character varying,
    error_since timestamp with time zone,
    error_observed_at timestamp with time zone,
    next_attempt timestamp with time zone,
    claim uuid,
    claim_until timestamp with time zone,
    CONSTRAINT github_review_refresh_error_kind CHECK (((error_kind)::text = ANY ((ARRAY['rate_limited'::character varying, 'unavailable'::character varying, 'access_denied'::character varying, 'source_changed'::character varying, 'processing_error'::character varying])::text[]))),
    CONSTRAINT github_review_refresh_error_state CHECK ((((error IS NULL) AND (error_kind IS NULL) AND (error_since IS NULL) AND (error_observed_at IS NULL)) OR ((error IS NOT NULL) AND (error_kind IS NOT NULL) AND (error_since IS NOT NULL) AND (error_observed_at IS NOT NULL)))),
    CONSTRAINT github_review_refresh_subject_kind_check CHECK (((subject_kind)::text = 'pull_request'::text))
);

CREATE TABLE github_subject (
    repository_id bigint NOT NULL,
    kind character varying NOT NULL,
    subject_key character varying NOT NULL,
    generation bigint NOT NULL,
    last_success_at timestamp with time zone,
    error_kind character varying,
    error character varying,
    error_since timestamp with time zone,
    error_observed_at timestamp with time zone,
    next_attempt timestamp with time zone,
    claim uuid,
    claim_until timestamp with time zone,
    CONSTRAINT github_subject_error_kind CHECK (((error_kind)::text = ANY ((ARRAY['rate_limited'::character varying, 'unavailable'::character varying, 'access_denied'::character varying, 'source_changed'::character varying, 'processing_error'::character varying])::text[]))),
    CONSTRAINT github_subject_error_state CHECK ((((error IS NULL) AND (error_kind IS NULL) AND (error_since IS NULL) AND (error_observed_at IS NULL)) OR ((error IS NOT NULL) AND (error_kind IS NOT NULL) AND (error_since IS NOT NULL) AND (error_observed_at IS NOT NULL)))),
    CONSTRAINT github_subject_kind CHECK (((kind)::text = ANY ((ARRAY['pull_request'::character varying, 'issue'::character varying, 'branch'::character varying, 'commit'::character varying])::text[])))
);

CREATE TABLE github_subject_revision (
    repository_id bigint NOT NULL,
    kind character varying NOT NULL,
    subject_key character varying NOT NULL,
    head_repository_id bigint NOT NULL,
    sha character varying NOT NULL
);

CREATE TABLE inbox (
    id uuid NOT NULL,
    owner_namespace character varying NOT NULL,
    owner_name character varying NOT NULL,
    destination_key character varying NOT NULL,
    session_id character varying NOT NULL,
    last_cursor bigint NOT NULL,
    acknowledged bigint NOT NULL,
    covered bigint NOT NULL,
    expired_through bigint NOT NULL,
    retired boolean NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    next_attempt timestamp with time zone,
    claim uuid,
    claim_until timestamp with time zone NOT NULL,
    delivery_error character varying,
    stale_check_at timestamp with time zone,
    destination_namespace character varying NOT NULL,
    destination_name character varying NOT NULL,
    destination_uid character varying NOT NULL,
    CONSTRAINT inbox_destination_fields_valid CHECK (((destination_namespace IS NOT NULL) AND ((char_length((destination_namespace)::text) >= 1) AND (char_length((destination_namespace)::text) <= 63)) AND (destination_name IS NOT NULL) AND ((char_length((destination_name)::text) >= 1) AND (char_length((destination_name)::text) <= 63)) AND (destination_uid IS NOT NULL) AND ((char_length((destination_uid)::text) >= 1) AND (char_length((destination_uid)::text) <= 128))))
);

CREATE TABLE message (
    id uuid NOT NULL,
    inbox_id uuid NOT NULL,
    cursor bigint NOT NULL,
    sender_namespace character varying NOT NULL,
    sender_name character varying NOT NULL,
    idempotency_key character varying NOT NULL,
    digest bytea NOT NULL
);

CREATE TABLE notice (
    inbox_id uuid NOT NULL,
    command_id uuid NOT NULL,
    through_cursor bigint NOT NULL,
    text character varying NOT NULL,
    attempted boolean NOT NULL,
    admitted boolean NOT NULL,
    confirmed boolean NOT NULL,
    error character varying,
    runner_cursor bigint NOT NULL,
    runner_entry bytea
);

CREATE TABLE subscription (
    id uuid NOT NULL,
    inbox_id uuid NOT NULL,
    idempotency_key character varying CONSTRAINT subscription_client_key_not_null NOT NULL,
    version integer NOT NULL,
    actions_after_sequence bigint,
    cancelled boolean NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    next_attempt timestamp with time zone,
    error character varying,
    generation bigint NOT NULL,
    github_start_position bigint,
    last_success_at timestamp with time zone,
    error_kind character varying,
    error_since timestamp with time zone,
    error_observed_at timestamp with time zone,
    github_app_id bigint,
    github_installation_id bigint,
    github_repository_id bigint,
    github_subject_kind character varying,
    github_subject_key character varying,
    github_mergeability_after_sequence bigint DEFAULT 0 NOT NULL,
    github_mergeability_initialized boolean DEFAULT false NOT NULL,
    github_commit_after_sequence bigint DEFAULT 0 NOT NULL,
    github_pr_state_after_sequence bigint DEFAULT 0 NOT NULL,
    creation_proto bytea NOT NULL,
    source_provider character varying NOT NULL,
    CONSTRAINT subscription_error_kind CHECK (((error_kind)::text = ANY ((ARRAY['rate_limited'::character varying, 'unavailable'::character varying, 'access_denied'::character varying, 'source_changed'::character varying, 'processing_error'::character varying])::text[]))),
    CONSTRAINT subscription_error_state CHECK ((((error IS NULL) AND (error_kind IS NULL) AND (error_since IS NULL) AND (error_observed_at IS NULL)) OR ((error IS NOT NULL) AND (error_kind IS NOT NULL) AND (error_since IS NOT NULL) AND (error_observed_at IS NOT NULL)))),
    CONSTRAINT subscription_source_provider CHECK (((source_provider IS NULL) OR ((source_provider)::text = ANY ((ARRAY['actions'::character varying, 'github'::character varying])::text[])))),
    CONSTRAINT subscription_typed_source_state CHECK (((source_provider IS NULL) OR
CASE source_provider
    WHEN 'actions'::text THEN ((actions_after_sequence IS NOT NULL) AND (github_start_position IS NULL) AND (github_app_id IS NULL) AND (github_installation_id IS NULL) AND (github_repository_id IS NULL) AND (github_subject_kind IS NULL) AND (github_subject_key IS NULL))
    WHEN 'github'::text THEN ((actions_after_sequence IS NULL) AND (github_start_position IS NOT NULL) AND (github_app_id IS NOT NULL) AND (github_installation_id IS NOT NULL) AND (github_repository_id IS NOT NULL) AND (github_subject_kind IS NOT NULL) AND (github_subject_key IS NOT NULL))
    ELSE false
END))
);

CREATE TABLE subscription_match (
    subscription_id uuid NOT NULL,
    cursor bigint NOT NULL
);

CREATE SEQUENCE github_repository_repository_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE github_repository_repository_id_seq OWNED BY github_repository.repository_id;

ALTER TABLE github_commit_event ALTER COLUMN sequence ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME github_commit_event_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE github_delivery ALTER COLUMN "position" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME github_delivery_position_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE github_mergeability_event ALTER COLUMN sequence ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME github_mergeability_event_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE github_pr_state_event ALTER COLUMN sequence ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME github_pr_state_event_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE ONLY github_repository ALTER COLUMN repository_id SET DEFAULT nextval('github_repository_repository_id_seq'::regclass);

ALTER TABLE ONLY entry
    ADD CONSTRAINT entry_pkey PRIMARY KEY (inbox_id, cursor);

ALTER TABLE ONLY github_commit_current
    ADD CONSTRAINT github_commit_current_pkey PRIMARY KEY (app_id, installation_id, repository_id, head_sha, event, entity_key);

ALTER TABLE ONLY github_commit_event
    ADD CONSTRAINT github_commit_event_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY github_commit_refresh
    ADD CONSTRAINT github_commit_refresh_pkey PRIMARY KEY (app_id, installation_id, repository_id, subject_kind, subject_key, event);

ALTER TABLE ONLY github_commit_subscription_snapshot
    ADD CONSTRAINT github_commit_subscription_snapshot_pkey PRIMARY KEY (subscription_id, sequence);

ALTER TABLE ONLY github_delivery
    ADD CONSTRAINT github_delivery_app_id_delivery_id_key UNIQUE (app_id, delivery_id);

ALTER TABLE ONLY github_delivery
    ADD CONSTRAINT github_delivery_pkey PRIMARY KEY ("position");

ALTER TABLE ONLY github_delivery
    ADD CONSTRAINT github_delivery_position_repository UNIQUE ("position", repository_id);

ALTER TABLE ONLY github_delivery_subject
    ADD CONSTRAINT github_delivery_subject_pkey PRIMARY KEY (delivery_position, repository_id, kind, subject_key);

ALTER TABLE ONLY github_installation
    ADD CONSTRAINT github_installation_pkey PRIMARY KEY (app_id, installation_id);

ALTER TABLE ONLY github_mergeability_event
    ADD CONSTRAINT github_mergeability_event_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY github_pr_state_current
    ADD CONSTRAINT github_pr_state_current_pkey PRIMARY KEY (app_id, installation_id, repository_id, pull_request_number, event, entity_id);

ALTER TABLE ONLY github_pr_state_event
    ADD CONSTRAINT github_pr_state_event_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY github_pr_state_subscription_snapshot
    ADD CONSTRAINT github_pr_state_subscription_snapshot_pkey PRIMARY KEY (subscription_id, sequence);

ALTER TABLE ONLY github_pull_request
    ADD CONSTRAINT github_pull_request_pkey PRIMARY KEY (repository_id, number);

ALTER TABLE ONLY github_repository_access
    ADD CONSTRAINT github_repository_access_pkey PRIMARY KEY (app_id, installation_id, repository_id);

ALTER TABLE ONLY github_repository
    ADD CONSTRAINT github_repository_pkey PRIMARY KEY (repository_id);

ALTER TABLE ONLY github_review_refresh
    ADD CONSTRAINT github_review_refresh_pkey PRIMARY KEY (app_id, installation_id, repository_id, subject_kind, subject_key);

ALTER TABLE ONLY github_subject
    ADD CONSTRAINT github_subject_pkey PRIMARY KEY (repository_id, kind, subject_key);

ALTER TABLE ONLY github_subject_revision
    ADD CONSTRAINT github_subject_revision_pkey PRIMARY KEY (repository_id, kind, subject_key, head_repository_id, sha);

ALTER TABLE ONLY inbox
    ADD CONSTRAINT inbox_owner_namespace_owner_name_destination_key_key UNIQUE (owner_namespace, owner_name, destination_key);

ALTER TABLE ONLY inbox
    ADD CONSTRAINT inbox_pkey PRIMARY KEY (id);

ALTER TABLE ONLY message
    ADD CONSTRAINT message_inbox_id_cursor_key UNIQUE (inbox_id, cursor);

ALTER TABLE ONLY message
    ADD CONSTRAINT message_inbox_id_sender_namespace_sender_name_idempotency_k_key UNIQUE (inbox_id, sender_namespace, sender_name, idempotency_key);

ALTER TABLE ONLY message
    ADD CONSTRAINT message_pkey PRIMARY KEY (id);

ALTER TABLE ONLY notice
    ADD CONSTRAINT notice_pkey PRIMARY KEY (inbox_id);

ALTER TABLE ONLY subscription
    ADD CONSTRAINT subscription_inbox_id_idempotency_key_key UNIQUE (inbox_id, idempotency_key);

ALTER TABLE ONLY subscription_match
    ADD CONSTRAINT subscription_match_pkey PRIMARY KEY (subscription_id, cursor);

ALTER TABLE ONLY subscription
    ADD CONSTRAINT subscription_pkey PRIMARY KEY (id);

ALTER TABLE ONLY entry
    ADD CONSTRAINT entry_inbox_id_fkey FOREIGN KEY (inbox_id) REFERENCES inbox(id) ON DELETE CASCADE;

ALTER TABLE ONLY github_commit_current
    ADD CONSTRAINT github_commit_current_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_commit_event
    ADD CONSTRAINT github_commit_event_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_commit_refresh
    ADD CONSTRAINT github_commit_refresh_app_id_installation_id_repository_id_fkey FOREIGN KEY (app_id, installation_id, repository_id) REFERENCES github_repository_access(app_id, installation_id, repository_id);

ALTER TABLE ONLY github_commit_refresh
    ADD CONSTRAINT github_commit_refresh_repository_id_subject_kind_subject_k_fkey FOREIGN KEY (repository_id, subject_kind, subject_key) REFERENCES github_subject(repository_id, kind, subject_key);

ALTER TABLE ONLY github_commit_subscription_snapshot
    ADD CONSTRAINT github_commit_subscription_snapshot_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES subscription(id) ON DELETE CASCADE;

ALTER TABLE ONLY github_delivery_subject
    ADD CONSTRAINT github_delivery_subject_delivery_position_repository_id_fkey FOREIGN KEY (delivery_position, repository_id) REFERENCES github_delivery("position", repository_id) ON DELETE CASCADE;

ALTER TABLE ONLY github_delivery_subject
    ADD CONSTRAINT github_delivery_subject_repository_id_kind_subject_key_fkey FOREIGN KEY (repository_id, kind, subject_key) REFERENCES github_subject(repository_id, kind, subject_key);

ALTER TABLE ONLY github_mergeability_event
    ADD CONSTRAINT github_mergeability_event_repository_id_pull_request_numbe_fkey FOREIGN KEY (repository_id, pull_request_number) REFERENCES github_pull_request(repository_id, number);

ALTER TABLE ONLY github_pr_state_current
    ADD CONSTRAINT github_pr_state_current_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_pr_state_event
    ADD CONSTRAINT github_pr_state_event_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_pr_state_subscription_snapshot
    ADD CONSTRAINT github_pr_state_subscription_snapshot_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES subscription(id) ON DELETE CASCADE;

ALTER TABLE ONLY github_pull_request
    ADD CONSTRAINT github_pull_request_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_repository_access
    ADD CONSTRAINT github_repository_access_app_id_installation_id_fkey FOREIGN KEY (app_id, installation_id) REFERENCES github_installation(app_id, installation_id);

ALTER TABLE ONLY github_repository_access
    ADD CONSTRAINT github_repository_access_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_review_refresh
    ADD CONSTRAINT github_review_refresh_app_id_installation_id_repository_id_fkey FOREIGN KEY (app_id, installation_id, repository_id) REFERENCES github_repository_access(app_id, installation_id, repository_id);

ALTER TABLE ONLY github_review_refresh
    ADD CONSTRAINT github_review_refresh_repository_id_subject_kind_subject_k_fkey FOREIGN KEY (repository_id, subject_kind, subject_key) REFERENCES github_subject(repository_id, kind, subject_key);

ALTER TABLE ONLY github_subject
    ADD CONSTRAINT github_subject_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_subject_revision
    ADD CONSTRAINT github_subject_revision_head_repository_id_fkey FOREIGN KEY (head_repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_subject_revision
    ADD CONSTRAINT github_subject_revision_repository_id_kind_subject_key_fkey FOREIGN KEY (repository_id, kind, subject_key) REFERENCES github_subject(repository_id, kind, subject_key);

ALTER TABLE ONLY message
    ADD CONSTRAINT message_inbox_id_cursor_fkey FOREIGN KEY (inbox_id, cursor) REFERENCES entry(inbox_id, cursor) ON DELETE CASCADE;

ALTER TABLE ONLY notice
    ADD CONSTRAINT notice_inbox_id_fkey FOREIGN KEY (inbox_id) REFERENCES inbox(id) ON DELETE CASCADE;

ALTER TABLE ONLY subscription
    ADD CONSTRAINT subscription_github_access FOREIGN KEY (github_app_id, github_installation_id, github_repository_id) REFERENCES github_repository_access(app_id, installation_id, repository_id);

ALTER TABLE ONLY subscription
    ADD CONSTRAINT subscription_github_subject FOREIGN KEY (github_repository_id, github_subject_kind, github_subject_key) REFERENCES github_subject(repository_id, kind, subject_key);

ALTER TABLE ONLY subscription
    ADD CONSTRAINT subscription_inbox_id_fkey FOREIGN KEY (inbox_id) REFERENCES inbox(id) ON DELETE CASCADE;

ALTER TABLE ONLY subscription_match
    ADD CONSTRAINT subscription_match_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES subscription(id) ON DELETE CASCADE;

CREATE UNIQUE INDEX entry_identity_actions_key ON entry USING btree (inbox_id, actions_request_id, actions_sequence) WHERE ((identity_provider)::text = 'actions'::text);

CREATE UNIQUE INDEX entry_identity_github_key ON entry USING btree (inbox_id, github_app_id, github_delivery_id, github_repository_id, github_event, github_action) NULLS NOT DISTINCT WHERE ((identity_provider)::text = 'github'::text);

CREATE UNIQUE INDEX entry_identity_github_observation_key ON entry USING btree (inbox_id, github_app_id, github_repository_id, github_event, github_observation_sequence) WHERE ((identity_provider)::text = 'github_observation'::text);

CREATE UNIQUE INDEX entry_identity_mergeability_sequence_key ON entry USING btree (inbox_id, github_app_id, github_repository_id, github_pull_request_number, github_mergeability_sequence) WHERE (((identity_provider)::text = 'github_mergeability'::text) AND (github_mergeability_sequence IS NOT NULL));

CREATE UNIQUE INDEX entry_identity_mergeability_snapshot_key ON entry USING btree (inbox_id, github_app_id, github_repository_id, github_pull_request_number, github_snapshot_version) WHERE (((identity_provider)::text = 'github_mergeability'::text) AND (github_snapshot_version IS NOT NULL));

CREATE UNIQUE INDEX entry_identity_message_key ON entry USING btree (inbox_id, message_id, message_sender_namespace, message_sender_name) WHERE ((identity_provider)::text = 'message'::text);

CREATE INDEX ix_github_commit_current_revision ON github_commit_current USING btree (app_id, repository_id, head_sha, event);

CREATE INDEX ix_github_commit_event_created_at ON github_commit_event USING btree (created_at, sequence);

CREATE INDEX ix_github_commit_event_revision ON github_commit_event USING btree (app_id, repository_id, head_sha, event, sequence);

CREATE INDEX ix_github_commit_subscription_snapshot_page ON github_commit_subscription_snapshot USING btree (subscription_id, sequence);

CREATE INDEX ix_github_delivery_head ON github_delivery USING btree (app_id, repository_id, head_sha);

CREATE INDEX ix_github_delivery_received_at ON github_delivery USING btree (received_at, "position");

CREATE INDEX ix_github_delivery_repository_id ON github_delivery USING btree (repository_id);

CREATE INDEX ix_github_delivery_subject_lookup ON github_delivery_subject USING btree (repository_id, kind, subject_key, delivery_position);

CREATE INDEX ix_github_mergeability_event_created_at ON github_mergeability_event USING btree (created_at, sequence);

CREATE INDEX ix_github_mergeability_event_subject ON github_mergeability_event USING btree (app_id, repository_id, pull_request_number, sequence);

CREATE INDEX ix_github_pr_state_current_subject ON github_pr_state_current USING btree (app_id, repository_id, pull_request_number, event);

CREATE INDEX ix_github_pr_state_event_created_at ON github_pr_state_event USING btree (created_at, sequence);

CREATE INDEX ix_github_pr_state_event_subject ON github_pr_state_event USING btree (app_id, repository_id, pull_request_number, event, sequence);

CREATE INDEX ix_github_pr_state_subscription_snapshot_page ON github_pr_state_subscription_snapshot USING btree (subscription_id, sequence);

CREATE INDEX ix_github_pull_request_base ON github_pull_request USING btree (repository_id, base_ref);

CREATE INDEX ix_inbox_next_attempt ON inbox USING btree (next_attempt);
CREATE UNIQUE INDEX inbox_session_id_unique ON inbox (session_id);

EXECUTE $ddl$CREATE FUNCTION notify_session_inbox_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected_session text;
    affected_inbox uuid;
BEGIN
    IF TG_TABLE_NAME = 'inbox' THEN
        affected_session := CASE WHEN TG_OP = 'DELETE' THEN OLD.session_id ELSE NEW.session_id END;
    ELSE
        affected_inbox := CASE WHEN TG_OP = 'DELETE' THEN OLD.inbox_id ELSE NEW.inbox_id END;
        SELECT session_id INTO affected_session FROM inbox WHERE id = affected_inbox;
    END IF;
    IF affected_session IS NOT NULL THEN
        PERFORM pg_notify('agentplane_notification_inbox', affected_session);
    END IF;
    RETURN NULL;
END $$;$ddl$;

EXECUTE $ddl$CREATE FUNCTION notify_github_subscription_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    changed_repository bigint;
    changed_kind text;
    changed_subject text;
    changed_app bigint;
    changed_installation bigint;
    affected_session text;
BEGIN
    IF TG_TABLE_NAME = 'github_installation' THEN
        changed_app := CASE WHEN TG_OP = 'DELETE' THEN OLD.app_id ELSE NEW.app_id END;
        changed_installation := CASE WHEN TG_OP = 'DELETE' THEN OLD.installation_id ELSE NEW.installation_id END;
        FOR affected_session IN
            SELECT DISTINCT i.session_id FROM inbox i JOIN subscription s ON s.inbox_id = i.id
            WHERE s.github_app_id = changed_app AND (s.github_installation_id = changed_installation OR EXISTS (
                SELECT 1 FROM github_subject_revision r JOIN github_repository_access a
                    ON a.repository_id = r.head_repository_id AND a.app_id = changed_app
                       AND a.installation_id = changed_installation
                WHERE r.repository_id = s.github_repository_id AND r.kind = s.github_subject_kind
                  AND r.subject_key = s.github_subject_key))
        LOOP
            PERFORM pg_notify('agentplane_notification_inbox', affected_session);
        END LOOP;
    ELSIF TG_TABLE_NAME = 'github_repository_access' THEN
        changed_repository := CASE WHEN TG_OP = 'DELETE' THEN OLD.repository_id ELSE NEW.repository_id END;
        changed_app := CASE WHEN TG_OP = 'DELETE' THEN OLD.app_id ELSE NEW.app_id END;
        FOR affected_session IN
            SELECT DISTINCT i.session_id FROM inbox i JOIN subscription s ON s.inbox_id = i.id
            WHERE s.github_app_id = changed_app AND
                (s.github_repository_id = changed_repository OR EXISTS (
                    SELECT 1 FROM github_subject_revision r
                    WHERE r.repository_id = s.github_repository_id AND r.kind = s.github_subject_kind
                      AND r.subject_key = s.github_subject_key AND r.head_repository_id = changed_repository))
        LOOP
            PERFORM pg_notify('agentplane_notification_inbox', affected_session);
        END LOOP;
    ELSE
        changed_repository := CASE WHEN TG_OP = 'DELETE' THEN OLD.repository_id ELSE NEW.repository_id END;
        IF TG_TABLE_NAME IN ('github_subject', 'github_subject_revision') THEN
            changed_kind := CASE WHEN TG_OP = 'DELETE' THEN OLD.kind ELSE NEW.kind END;
            changed_subject := CASE WHEN TG_OP = 'DELETE' THEN OLD.subject_key ELSE NEW.subject_key END;
        ELSE
            changed_kind := CASE WHEN TG_OP = 'DELETE' THEN OLD.subject_kind ELSE NEW.subject_kind END;
            changed_subject := CASE WHEN TG_OP = 'DELETE' THEN OLD.subject_key ELSE NEW.subject_key END;
        END IF;
        FOR affected_session IN
            SELECT DISTINCT i.session_id FROM inbox i JOIN subscription s ON s.inbox_id = i.id
            WHERE s.github_repository_id = changed_repository AND s.github_subject_kind = changed_kind
              AND s.github_subject_key = changed_subject
        LOOP
            PERFORM pg_notify('agentplane_notification_inbox', affected_session);
        END LOOP;
    END IF;
    RETURN NULL;
END $$;$ddl$;

EXECUTE 'CREATE TRIGGER inbox_watch_change AFTER INSERT OR UPDATE OR DELETE ON inbox
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();';
EXECUTE 'CREATE TRIGGER entry_watch_change AFTER INSERT OR UPDATE OR DELETE ON entry
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();';
EXECUTE 'CREATE TRIGGER notice_watch_change AFTER INSERT OR UPDATE OR DELETE ON notice
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();';
EXECUTE 'CREATE TRIGGER subscription_watch_change AFTER INSERT OR UPDATE OR DELETE ON subscription
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();';
EXECUTE 'CREATE TRIGGER github_installation_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_installation
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();';
EXECUTE 'CREATE TRIGGER github_access_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_repository_access
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();';
EXECUTE 'CREATE TRIGGER github_subject_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_subject
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();';
EXECUTE 'CREATE TRIGGER github_subject_revision_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_subject_revision
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();';
EXECUTE 'CREATE TRIGGER github_commit_refresh_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_commit_refresh
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();';
EXECUTE 'CREATE TRIGGER github_review_refresh_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_review_refresh
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();';
END
$notification_baseline$;
-- +goose StatementEnd
