-- +goose Up
-- This is the current schema for an empty database. A completed version-9 database is adopted only
-- after its atomic cutover marker and final native schema validate; migration 10 does no row copy.
-- Retain aggregate GitHub CI rows: their sequence ids overlap native check-run/status ids but name different events.
-- +goose StatementBegin
DO $notification_final_schema$
DECLARE
    current_version BIGINT;
    applied_versions INTEGER;
BEGIN
    IF to_regclass(format('%I.%I', current_schema(), 'notification_storage_cutover')) IS NOT NULL THEN
        IF to_regclass(format('%I.%I', current_schema(), 'notification_service_goose_version')) IS NULL THEN
            RAISE EXCEPTION 'Notification storage cutover history is missing; run the previous image to finish migration 9';
        END IF;

        WITH latest AS (
            SELECT DISTINCT ON (version_id) version_id, is_applied
            FROM notification_service_goose_version
            WHERE version_id BETWEEN 1 AND 9
            ORDER BY version_id, id DESC
        )
        SELECT count(*) FILTER (WHERE is_applied), max(version_id) FILTER (WHERE is_applied)
        INTO applied_versions, current_version
        FROM latest;
        IF applied_versions <> 9 OR current_version <> 9 THEN
            RAISE EXCEPTION 'Notification schema cutover is complete but Goose is not at version 9; run the previous image to finish migration 9';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'notification_storage_cutover'
                AND column_name IN ('singleton', 'phase')
            GROUP BY table_name HAVING count(*) = 2
        )
        THEN
            RAISE EXCEPTION 'Notification storage cutover is incomplete; run the previous image to finish migration 9';
        END IF;
        IF (SELECT count(*) FROM notification_storage_cutover) <> 1
            OR (SELECT count(*) FROM notification_storage_cutover WHERE singleton AND phase = 'complete') <> 1
        THEN
            RAISE EXCEPTION 'Notification storage cutover is incomplete; run the previous image to finish migration 9';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM (VALUES
                ('inbox', 'destination_namespace'),
                ('inbox', 'destination_name'),
                ('inbox', 'destination_uid'),
                ('subscription', 'creation_proto'),
                ('subscription', 'source_provider'),
                ('entry', 'identity_provider'),
                ('entry', 'actions_request_id'),
                ('entry', 'actions_sequence'),
                ('entry', 'github_app_id'),
                ('entry', 'github_delivery_id'),
                ('entry', 'github_repository_id'),
                ('entry', 'github_event'),
                ('entry', 'github_action'),
                ('entry', 'github_pull_request_number'),
                ('entry', 'github_mergeability_sequence'),
                ('entry', 'github_snapshot_version'),
                ('entry', 'github_observation_sequence'),
                ('entry', 'message_id'),
                ('entry', 'message_sender_namespace'),
                ('entry', 'message_sender_name'),
                ('entry', 'action_state'),
                ('entry', 'action_at'),
                ('entry', 'action_actor_kind'),
                ('entry', 'action_actor_namespace'),
                ('entry', 'action_actor_name'),
                ('entry', 'action_actor_issuer'),
                ('entry', 'action_actor_subject'),
                ('entry', 'mergeability_repository'),
                ('entry', 'mergeability_base_ref'),
                ('entry', 'mergeability_head_sha'),
                ('entry', 'mergeability_base_sha'),
                ('entry', 'mergeability_previous'),
                ('entry', 'mergeability_current'),
                ('entry', 'mergeability_observed_at'),
                ('entry', 'mergeability_observed_at_submicro_nanos'),
                ('entry', 'merge_commit_sha'),
                ('entry', 'merge_commit_base_sha'),
                ('entry', 'merge_commit_head_sha'),
                ('entry', 'message_body'),
                ('entry', 'message_from_session')
            ) AS expected(table_name, column_name)
            WHERE NOT EXISTS (
                SELECT 1 FROM information_schema.columns actual
                WHERE actual.table_schema = current_schema()
                    AND actual.table_name = expected.table_name
                    AND actual.column_name = expected.column_name
            )
        ) OR EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
                AND ((table_name = 'inbox' AND column_name = 'destination_ref')
                    OR (table_name = 'subscription' AND column_name = 'creation')
                    OR (table_name = 'entry' AND column_name = 'event'))
        ) THEN
            RAISE EXCEPTION 'Notification final native schema is incomplete; run the previous image to finish migration 9';
        END IF;

        IF (SELECT count(*)
            FROM pg_index i
            JOIN pg_class index_relation ON index_relation.oid = i.indexrelid
            WHERE i.indrelid = to_regclass(format('%I.%I', current_schema(), 'entry'))
                AND index_relation.relname IN (
                    'entry_identity_actions_key', 'entry_identity_github_key',
                    'entry_identity_github_observation_key', 'entry_identity_mergeability_sequence_key',
                    'entry_identity_mergeability_snapshot_key', 'entry_identity_message_key'
                ) AND i.indisvalid AND i.indisready AND i.indisunique
        ) <> 6 THEN
            RAISE EXCEPTION 'Notification final identity indexes are incomplete; run the previous image to finish migration 9';
        END IF;

        IF to_regclass(format('%I.%I', current_schema(), 'notification_storage_backfill_progress')) IS NOT NULL
            OR to_regclass(format('%I.%I', current_schema(), 'notification_event_identity_backfill')) IS NOT NULL
            OR to_regclass(format('%I.%I', current_schema(), 'notification_entry_content_backfill')) IS NOT NULL
            OR to_regclass(format('%I.%I', current_schema(), 'notification_entry_legacy_cleanup')) IS NOT NULL
        THEN
            RAISE EXCEPTION 'Notification storage cutover checkpoints remain; run the previous image to finish migration 9';
        END IF;

        DROP TABLE notification_storage_cutover;
        RETURN;
    END IF;

    IF to_regclass(format('%I.%I', current_schema(), 'notification_service_goose_version')) IS NULL THEN
        RAISE EXCEPTION 'Goose history is missing while applying Notification migration 10';
    END IF;

    WITH latest AS (
        SELECT DISTINCT ON (version_id) version_id, is_applied
        FROM notification_service_goose_version
        ORDER BY version_id, id DESC
    )
    SELECT COALESCE(max(version_id) FILTER (WHERE is_applied), 0)
    INTO current_version
    FROM latest;
    IF current_version <> 0 THEN
        RAISE EXCEPTION 'Existing Notification schema is not fresh; run the previous image to finish migration 9 before migration 10';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_class relation
        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = current_schema()
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
            AND relation.relname NOT IN (
                'notification_service_goose_version',
                'notification_service_goose_version_id_seq'
            )
    ) THEN
        RAISE EXCEPTION 'Existing Notification objects have no completed migration 9 marker; run the previous image to finish migration 9';
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

CREATE TABLE github_ci_current (
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    head_sha character varying NOT NULL,
    event character varying NOT NULL,
    entity_key character varying NOT NULL,
    last_sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT github_ci_current_event_check CHECK (((event)::text = ANY ((ARRAY['check_run'::character varying, 'status'::character varying])::text[])))
);

CREATE TABLE github_ci_event (
    sequence bigint NOT NULL,
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    head_sha character varying NOT NULL,
    event character varying NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT github_ci_event_event_check CHECK (((event)::text = ANY ((ARRAY['check_run'::character varying, 'status'::character varying])::text[])))
);

CREATE TABLE github_ci_refresh (
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
    CONSTRAINT github_ci_refresh_error_kind CHECK (((error_kind)::text = ANY ((ARRAY['rate_limited'::character varying, 'unavailable'::character varying, 'access_denied'::character varying, 'source_changed'::character varying, 'processing_error'::character varying])::text[]))),
    CONSTRAINT github_ci_refresh_error_state CHECK ((((error IS NULL) AND (error_kind IS NULL) AND (error_since IS NULL) AND (error_observed_at IS NULL)) OR ((error IS NOT NULL) AND (error_kind IS NOT NULL) AND (error_since IS NOT NULL) AND (error_observed_at IS NOT NULL)))),
    CONSTRAINT github_ci_refresh_subject_kind_check CHECK (((subject_kind)::text = 'pull_request'::text))
);

CREATE TABLE github_ci_subscription_snapshot (
    subscription_id uuid NOT NULL,
    app_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repository_id bigint NOT NULL,
    event character varying NOT NULL,
    sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT github_ci_subscription_snapshot_event_check CHECK (((event)::text = ANY ((ARRAY['check_run'::character varying, 'status'::character varying])::text[])))
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
    ci_normalized boolean DEFAULT false NOT NULL,
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
    github_ci_after_sequence bigint DEFAULT 0 NOT NULL,
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

ALTER TABLE github_ci_event ALTER COLUMN sequence ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME github_ci_event_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

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

ALTER TABLE ONLY github_ci_current
    ADD CONSTRAINT github_ci_current_pkey PRIMARY KEY (app_id, installation_id, repository_id, head_sha, event, entity_key);

ALTER TABLE ONLY github_ci_event
    ADD CONSTRAINT github_ci_event_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY github_ci_refresh
    ADD CONSTRAINT github_ci_refresh_pkey PRIMARY KEY (app_id, installation_id, repository_id, subject_kind, subject_key);

ALTER TABLE ONLY github_ci_subscription_snapshot
    ADD CONSTRAINT github_ci_subscription_snapshot_pkey PRIMARY KEY (subscription_id, sequence);

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

ALTER TABLE ONLY github_ci_current
    ADD CONSTRAINT github_ci_current_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_ci_event
    ADD CONSTRAINT github_ci_event_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES github_repository(repository_id);

ALTER TABLE ONLY github_ci_refresh
    ADD CONSTRAINT github_ci_refresh_app_id_installation_id_repository_id_fkey FOREIGN KEY (app_id, installation_id, repository_id) REFERENCES github_repository_access(app_id, installation_id, repository_id);

ALTER TABLE ONLY github_ci_refresh
    ADD CONSTRAINT github_ci_refresh_repository_id_subject_kind_subject_key_fkey FOREIGN KEY (repository_id, subject_kind, subject_key) REFERENCES github_subject(repository_id, kind, subject_key);

ALTER TABLE ONLY github_ci_subscription_snapshot
    ADD CONSTRAINT github_ci_subscription_snapshot_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES subscription(id) ON DELETE CASCADE;

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

CREATE INDEX ix_github_ci_current_revision ON github_ci_current USING btree (app_id, repository_id, head_sha, event);

CREATE INDEX ix_github_ci_event_created_at ON github_ci_event USING btree (created_at, sequence);

CREATE INDEX ix_github_ci_event_revision ON github_ci_event USING btree (app_id, repository_id, head_sha, event, sequence);

CREATE INDEX ix_github_ci_subscription_snapshot_page ON github_ci_subscription_snapshot USING btree (subscription_id, sequence);

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
END
$notification_final_schema$;
-- +goose StatementEnd
