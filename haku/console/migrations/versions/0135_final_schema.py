"""Create the final Haku console schema, retaining deployed revision 0135.

Already-at-head databases apply nothing. Older databases must first reach this
revision with the pre-squash image. These definitions are independent of live ORM
metadata; the original-chain schema capture is used only as test evidence.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from agentplane.indexing.vector_type import HalfVector

revision: str = "0135"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_AGENT_STATUS = postgresql.ENUM(
    "draft", "active", "abandoned", "disabled", "deleted", name="agent_status", create_type=False
)
_CLIENT_REGISTRATION_KIND = postgresql.ENUM(
    "oauth_proxy_unclassified", "dcr", "cimd", "preregistered", name="client_registration_kind", create_type=False
)
_CREDENTIAL_BINDING_STATUS = postgresql.ENUM(
    "issuing", "issued", "active", "revoked", "expired", "failed", name="credential_binding_status", create_type=False
)
_CREDENTIAL_KIND = postgresql.ENUM("oauth", "static", name="credential_kind", create_type=False)
_ENROLLMENT_PHASE = postgresql.ENUM(
    "awaiting_browser",
    "awaiting_approval",
    "allowed",
    "exchanging",
    "completed",
    "denied",
    "expired",
    "failed",
    name="enrollment_phase",
    create_type=False,
)
_OPERATOR_STATUS = postgresql.ENUM("active", "disabled", name="operator_status", create_type=False)
_TOOL_CALL_STATUS = postgresql.ENUM(
    "pending_approval", "running", "ok", "error", "denied", "withdrawn", name="tool_call_status", create_type=False
)


def upgrade() -> None:
    conn = op.get_bind()
    check_bodies = conn.scalar(sa.text("SHOW check_function_bodies"))
    op.execute("SET LOCAL check_function_bodies = false")
    op.execute(sa.schema.CreateSchema("recall_index"))
    _create_types()
    _create_functions()
    _create_authority_tables()
    _create_console_tables()
    _create_recall_tables()
    _preserve_deployed_constraint_names()
    _create_foreign_keys()
    _create_indexes()
    _create_triggers()
    conn.execute(sa.text("SELECT set_config('check_function_bodies', :value, true)"), {"value": check_bodies})
    # The git index remains; the retired chat index must not return.
    op.bulk_insert(
        sa.table(
            "indexes", sa.column("index_id", sa.Text()), sa.column("index_type", sa.Text()), schema="recall_index"
        ),
        [{"index_id": "haku-state", "index_type": "git"}],
    )


def downgrade() -> None:
    raise RuntimeError("0135 is the forward-only Haku console baseline")


def _create_types() -> None:
    _AGENT_STATUS.create(op.get_bind(), checkfirst=False)
    _CLIENT_REGISTRATION_KIND.create(op.get_bind(), checkfirst=False)
    _CREDENTIAL_BINDING_STATUS.create(op.get_bind(), checkfirst=False)
    _CREDENTIAL_KIND.create(op.get_bind(), checkfirst=False)
    _ENROLLMENT_PHASE.create(op.get_bind(), checkfirst=False)
    _OPERATOR_STATUS.create(op.get_bind(), checkfirst=False)
    _TOOL_CALL_STATUS.create(op.get_bind(), checkfirst=False)


def _create_functions() -> None:
    op.execute(r"""
CREATE FUNCTION public.haku_0009_agent_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'draft' THEN
                    RAISE EXCEPTION 'activated Agent records are tombstoned, not deleted'
                        USING ERRCODE = '23514';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF ROW(NEW.agent_id, NEW.owner_operator_id, NEW.created_at)
                   IS DISTINCT FROM ROW(OLD.agent_id, OLD.owner_operator_id, OLD.created_at) THEN
                    RAISE EXCEPTION 'Agent identity and owner are immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF OLD.activated_at IS NOT NULL
                   AND NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
                    RAISE EXCEPTION 'Agent activation evidence is immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF OLD.last_seen_at IS NOT NULL
                   AND (NEW.last_seen_at IS NULL OR NEW.last_seen_at < OLD.last_seen_at) THEN
                    RAISE EXCEPTION 'Agent last-seen evidence is monotonic'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.status <> OLD.status AND NOT (
                    (OLD.status = 'draft' AND NEW.status = 'active')
                    OR (OLD.status = 'draft' AND NEW.status = 'abandoned')
                    OR (OLD.status = 'active' AND NEW.status IN ('disabled', 'deleted'))
                    OR (OLD.status = 'disabled' AND NEW.status = 'deleted')
                    OR (
                        OLD.status = 'disabled' AND NEW.status = 'active'
                        AND EXISTS (
                            SELECT 1 FROM credential_bindings
                            WHERE agent_id = NEW.agent_id
                              AND kind = 'static'
                              AND status = 'active'
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM credential_bindings
                            WHERE agent_id = NEW.agent_id AND kind <> 'static'
                        )
                    )
                ) THEN
                    RAISE EXCEPTION 'illegal Agent status transition: % -> %',
                        OLD.status, NEW.status USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_agent_name_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            owning_agent_status TEXT;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.agent_id IS NOT NULL THEN
                    SELECT status::TEXT INTO owning_agent_status
                    FROM agents WHERE agent_id = OLD.agent_id;
                    IF owning_agent_status IS DISTINCT FROM 'draft' THEN
                        RAISE EXCEPTION 'activated and historical Agent names remain reserved'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'INSERT'
               AND NEW.originating_interaction_id IS NOT NULL
               AND NEW.pending_interaction_id IS NULL THEN
                RAISE EXCEPTION 'interaction-originated Agent names must begin as pending reservations'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF ROW(NEW.reservation_id, NEW.display_name, NEW.display_name_key,
                       NEW.originating_interaction_id, NEW.created_at)
                   IS DISTINCT FROM
                   ROW(OLD.reservation_id, OLD.display_name, OLD.display_name_key,
                       OLD.originating_interaction_id, OLD.created_at) THEN
                    RAISE EXCEPTION 'Agent display-name reservations are immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF ROW(NEW.pending_interaction_id, NEW.agent_id, NEW.activated_at)
                   IS DISTINCT FROM
                   ROW(OLD.pending_interaction_id, OLD.agent_id, OLD.activated_at)
                   AND NOT (
                       OLD.pending_interaction_id IS NOT NULL
                       AND OLD.agent_id IS NULL
                       AND OLD.activated_at IS NULL
                       AND NEW.pending_interaction_id IS NULL
                       AND NEW.agent_id IS NOT NULL
                       AND NEW.activated_at IS NOT NULL
                   ) THEN
                    RAISE EXCEPTION 'Agent name ownership may only transfer from interaction to Agent'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_assert_binding_activation(target_binding_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE
            binding_agent UUID;
            binding_status TEXT;
            binding_generation BIGINT;
            binding_predecessor UUID;
            agent_status TEXT;
            maximum_generation BIGINT;
            predecessor_status TEXT;
        BEGIN
            SELECT binding.agent_id, binding.status::TEXT, binding.generation,
                   binding.supersedes_binding_id, agent.status::TEXT
            INTO binding_agent, binding_status, binding_generation,
                 binding_predecessor, agent_status
            FROM credential_bindings AS binding
            JOIN agents AS agent ON agent.agent_id = binding.agent_id
            WHERE binding.binding_id = target_binding_id;
            IF NOT FOUND OR binding_status <> 'active' THEN
                RETURN;
            END IF;
            IF agent_status <> 'active' THEN
                RAISE EXCEPTION 'only an active Agent may own an active binding'
                    USING ERRCODE = '23514';
            END IF;
            SELECT max(generation) INTO maximum_generation
            FROM credential_bindings WHERE agent_id = binding_agent;
            IF binding_generation <> maximum_generation THEN
                RAISE EXCEPTION 'a stale binding generation cannot become active'
                    USING ERRCODE = '23514';
            END IF;
            IF binding_predecessor IS NULL THEN
                IF binding_generation <> 1 THEN
                    RAISE EXCEPTION 'an initial active binding must be generation one'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                SELECT status::TEXT INTO predecessor_status
                FROM credential_bindings WHERE binding_id = binding_predecessor;
                IF predecessor_status NOT IN ('revoked', 'expired', 'failed') THEN
                    RAISE EXCEPTION 'replacement activation must terminally close its predecessor'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_assert_binding_subtype(target_binding_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE
            binding_kind TEXT;
            grant_count BIGINT;
            static_count BIGINT;
        BEGIN
            SELECT kind::TEXT INTO binding_kind
            FROM credential_bindings WHERE binding_id = target_binding_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT count(*) INTO grant_count
            FROM authorization_grants WHERE binding_id = target_binding_id;
            SELECT count(*) INTO static_count
            FROM static_credentials WHERE binding_id = target_binding_id;
            IF (binding_kind = 'oauth' AND (grant_count <> 1 OR static_count <> 0))
               OR (binding_kind = 'static' AND (grant_count <> 0 OR static_count <> 1)) THEN
                RAISE EXCEPTION 'CredentialBinding must own exactly its enum-selected subtype'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_assert_correlation_reservation(target_interaction_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE
            reservation_count BIGINT;
        BEGIN
            SELECT count(*) INTO reservation_count
            FROM enrollment_correlation_reservations
            WHERE interaction_id = target_interaction_id;
            IF reservation_count <> 1 THEN
                RAISE EXCEPTION 'EnrollmentInteraction must own exactly one correlation reservation'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_assert_grant_consistency(target_grant_id uuid, require_initial_state boolean) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE
            binding_kind TEXT;
            binding_status TEXT;
            binding_agent UUID;
            binding_generation BIGINT;
            binding_predecessor UUID;
            agent_owner UUID;
            agent_status TEXT;
            owner_status TEXT;
            authorizing_operator UUID;
            browser_operator UUID;
            grant_client UUID;
            interaction_client UUID;
            interaction_phase TEXT;
            reconnect_agent UUID;
            reconnect_binding UUID;
            reconnect_binding_kind TEXT;
            reconnect_binding_status TEXT;
            current_name_origin UUID;
            grant_interaction UUID;
            scopes_are_narrowed BOOLEAN;
        BEGIN
            SELECT binding.kind::TEXT,
                   binding.status::TEXT,
                   binding.agent_id,
                   binding.generation,
                   binding.supersedes_binding_id,
                   agent.owner_operator_id,
                   agent.status::TEXT,
                   owner.status::TEXT,
                   authorizing_anchor.operator_id,
                   browser_anchor.operator_id,
                   auth_grant.client_software_id,
                   interaction.client_software_id,
                   interaction.phase::TEXT,
                   interaction.reconnect_agent_id,
                   interaction.reconnect_predecessor_binding_id,
                   reconnect_predecessor.kind::TEXT,
                   reconnect_predecessor.status::TEXT,
                   current_name.originating_interaction_id,
                   interaction.interaction_id,
                   auth_grant.allowed_scopes <@ interaction.requested_scopes
            INTO binding_kind, binding_status, binding_agent, binding_generation, binding_predecessor,
                 agent_owner, agent_status, owner_status, authorizing_operator,
                 browser_operator, grant_client, interaction_client, interaction_phase,
                 reconnect_agent, reconnect_binding, reconnect_binding_kind,
                 reconnect_binding_status, current_name_origin, grant_interaction,
                 scopes_are_narrowed
            FROM authorization_grants AS auth_grant
            JOIN credential_bindings AS binding ON binding.binding_id = auth_grant.binding_id
            JOIN agents AS agent ON agent.agent_id = binding.agent_id
            JOIN agent_name_reservations AS current_name
              ON current_name.reservation_id = agent.current_name_reservation_id
            JOIN operators AS owner ON owner.operator_id = agent.owner_operator_id
            JOIN oidc_identities AS authorizing_identity
              ON authorizing_identity.identity_id = auth_grant.authorizing_identity_id
            JOIN identity_anchors AS authorizing_anchor
              ON authorizing_anchor.anchor_id = authorizing_identity.anchor_id
            JOIN enrollment_interactions AS interaction
              ON interaction.interaction_id = auth_grant.enrollment_interaction_id
            LEFT JOIN oidc_identities AS browser_identity
              ON browser_identity.identity_id = interaction.browser_identity_id
            LEFT JOIN identity_anchors AS browser_anchor
              ON browser_anchor.anchor_id = browser_identity.anchor_id
            LEFT JOIN credential_bindings AS reconnect_predecessor
              ON reconnect_predecessor.binding_id = interaction.reconnect_predecessor_binding_id
            WHERE auth_grant.grant_id = target_grant_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;

            IF binding_kind <> 'oauth' THEN
                RAISE EXCEPTION 'AuthorizationGrant requires an OAuth binding'
                    USING ERRCODE = '23514';
            END IF;
            IF require_initial_state AND binding_status <> 'issuing' THEN
                RAISE EXCEPTION 'AuthorizationGrant must begin on an issuing OAuth binding'
                    USING ERRCODE = '23514';
            END IF;
            IF owner_status <> 'active'
               OR authorizing_operator IS DISTINCT FROM agent_owner
               OR browser_operator IS DISTINCT FROM agent_owner THEN
                RAISE EXCEPTION 'grant, browser identity, and Agent must resolve to one active Operator'
                    USING ERRCODE = '23514';
            END IF;
            IF grant_client <> interaction_client THEN
                RAISE EXCEPTION 'grant client software must match its interaction'
                    USING ERRCODE = '23514';
            END IF;
            IF scopes_are_narrowed IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'grant scopes cannot broaden the requested client-facing scopes'
                    USING ERRCODE = '23514';
            END IF;
            IF interaction_phase NOT IN ('exchanging', 'completed', 'expired', 'failed') THEN
                RAISE EXCEPTION 'resulting grant requires an exchanging or closed interaction'
                    USING ERRCODE = '23514';
            END IF;

            IF reconnect_agent IS NULL THEN
                IF binding_predecessor IS NOT NULL OR binding_generation <> 1
                   OR current_name_origin IS DISTINCT FROM grant_interaction
                   OR agent_status NOT IN ('draft', 'active')
                   OR (require_initial_state AND agent_status <> 'draft') THEN
                    RAISE EXCEPTION 'create-new enrollment requires a draft Agent first binding'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF binding_agent <> reconnect_agent OR binding_predecessor <> reconnect_binding
                  OR reconnect_binding_kind <> 'oauth'
                  OR (require_initial_state AND reconnect_binding_status <> 'active')
                  OR agent_status <> 'active' THEN
                RAISE EXCEPTION 'reconnect grant must replace the selected binding on its active Agent'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_assert_interaction_aggregate(target_interaction_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE
            interaction_phase TEXT;
            reconnect_agent UUID;
            reconnect_binding UUID;
            browser_operator UUID;
            reconnect_owner UUID;
            reconnect_status TEXT;
            reconnect_kind TEXT;
            pending_name_count BIGINT;
            grant_count BIGINT;
        BEGIN
            SELECT interaction.phase::TEXT,
                   interaction.reconnect_agent_id,
                   interaction.reconnect_predecessor_binding_id,
                   browser_anchor.operator_id
            INTO interaction_phase, reconnect_agent, reconnect_binding, browser_operator
            FROM enrollment_interactions AS interaction
            LEFT JOIN oidc_identities AS browser_identity
              ON browser_identity.identity_id = interaction.browser_identity_id
            LEFT JOIN identity_anchors AS browser_anchor
              ON browser_anchor.anchor_id = browser_identity.anchor_id
            WHERE interaction.interaction_id = target_interaction_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;

            SELECT count(*) INTO pending_name_count
            FROM agent_name_reservations
            WHERE pending_interaction_id = target_interaction_id;
            SELECT count(*) INTO grant_count
            FROM authorization_grants
            WHERE enrollment_interaction_id = target_interaction_id;

            IF interaction_phase IN ('awaiting_browser', 'awaiting_approval') THEN
                IF pending_name_count <> 0 OR grant_count <> 0 THEN
                    RAISE EXCEPTION 'pre-decision interaction cannot own a name or grant'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF interaction_phase = 'allowed' THEN
                IF grant_count <> 0 OR
                   ((reconnect_agent IS NULL AND pending_name_count <> 1)
                    OR (reconnect_agent IS NOT NULL AND pending_name_count <> 0)) THEN
                    RAISE EXCEPTION 'Allowed interaction must be exactly create-new or reconnect'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF interaction_phase IN ('exchanging', 'completed') THEN
                IF pending_name_count <> 0 OR grant_count <> 1 THEN
                    RAISE EXCEPTION 'exchanging/completed interaction must own one resulting grant'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF interaction_phase = 'denied' THEN
                IF pending_name_count <> 0 OR grant_count <> 0 THEN
                    RAISE EXCEPTION 'denied interaction cannot own a name or grant'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF interaction_phase IN ('expired', 'failed') THEN
                IF pending_name_count <> 0 OR grant_count > 1 THEN
                    RAISE EXCEPTION 'closed interaction cannot retain a pending name'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'unknown EnrollmentInteraction phase: %', interaction_phase
                    USING ERRCODE = '23514';
            END IF;

            IF reconnect_agent IS NOT NULL THEN
                SELECT agent.owner_operator_id, binding.status::TEXT, binding.kind::TEXT
                INTO reconnect_owner, reconnect_status, reconnect_kind
                FROM agents AS agent
                JOIN credential_bindings AS binding
                  ON binding.agent_id = agent.agent_id
                 AND binding.binding_id = reconnect_binding
                WHERE agent.agent_id = reconnect_agent;
                IF NOT FOUND OR reconnect_owner IS DISTINCT FROM browser_operator THEN
                    RAISE EXCEPTION 'reconnect target must belong to the browser Operator'
                        USING ERRCODE = '23514';
                END IF;
                IF interaction_phase = 'allowed'
                   AND (reconnect_status <> 'active' OR reconnect_kind <> 'oauth') THEN
                    RAISE EXCEPTION 'new reconnect authorization must target an active OAuth binding'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_assert_name_promotion(target_reservation_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE
            origin_interaction UUID;
            pending_interaction UUID;
            owning_agent UUID;
            matching_grants BIGINT;
        BEGIN
            SELECT originating_interaction_id, pending_interaction_id, agent_id
            INTO origin_interaction, pending_interaction, owning_agent
            FROM agent_name_reservations
            WHERE reservation_id = target_reservation_id;
            IF NOT FOUND OR origin_interaction IS NULL OR pending_interaction IS NOT NULL THEN
                RETURN;
            END IF;
            SELECT count(*) INTO matching_grants
            FROM authorization_grants AS auth_grant
            JOIN credential_bindings AS binding ON binding.binding_id = auth_grant.binding_id
            WHERE auth_grant.enrollment_interaction_id = origin_interaction
              AND binding.agent_id = owning_agent;
            IF matching_grants <> 1 THEN
                RAISE EXCEPTION 'promoted Agent name must belong to its interaction resulting Agent'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_assert_tool_call_principal(target_tool_call_id text) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE
            principal_count BIGINT;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM mcp_tool_calls WHERE tool_call_id = target_tool_call_id
            ) THEN
                RETURN;
            END IF;
            SELECT count(*) INTO principal_count
            FROM mcp_tool_call_principals
            WHERE tool_call_id = target_tool_call_id;
            IF principal_count <> 1 THEN
                RAISE EXCEPTION 'every tool call must own exactly one ToolCallPrincipal'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_authorization_grant_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF ROW(NEW.grant_id, NEW.binding_id, NEW.authorizing_identity_id,
                   NEW.client_software_id, NEW.enrollment_interaction_id,
                   NEW.allowed_scopes, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.grant_id, OLD.binding_id, OLD.authorizing_identity_id,
                   OLD.client_software_id, OLD.enrollment_interaction_id,
                   OLD.allowed_scopes, OLD.created_at) THEN
                RAISE EXCEPTION 'AuthorizationGrant provenance and scopes are immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.initial_access_jti IS NOT NULL
               AND NEW.initial_access_jti IS DISTINCT FROM OLD.initial_access_jti THEN
                RAISE EXCEPTION 'initial access-token evidence cannot be changed or cleared'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.initial_refresh_jti IS NOT NULL
               AND NEW.initial_refresh_jti IS DISTINCT FROM OLD.initial_refresh_jti THEN
                RAISE EXCEPTION 'initial refresh-token evidence cannot be changed or cleared'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.token_family_persisted_at IS NOT NULL
               AND NEW.token_family_persisted_at IS DISTINCT FROM OLD.token_family_persisted_at THEN
                RAISE EXCEPTION 'token-family persistence evidence cannot be changed or cleared'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_agent_active_bindings() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            active_binding UUID;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            IF NEW.status <> 'active' THEN
                SELECT binding_id INTO active_binding
                FROM credential_bindings
                WHERE agent_id = NEW.agent_id AND status = 'active'
                LIMIT 1;
                IF active_binding IS NOT NULL THEN
                    RAISE EXCEPTION 'non-active Agent cannot retain an active binding'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            IF NEW.status = 'abandoned' AND (
                EXISTS (
                    SELECT 1 FROM credential_bindings
                    WHERE agent_id = NEW.agent_id
                      AND status NOT IN ('revoked', 'expired', 'failed')
                )
                OR NOT EXISTS (
                    SELECT 1 FROM credential_bindings
                    WHERE agent_id = NEW.agent_id
                      AND status IN ('revoked', 'expired', 'failed')
                )
            ) THEN
                RAISE EXCEPTION 'abandoned Agent requires terminal bindings and no live binding'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'draft' AND NEW.status = 'active'
               AND NOT EXISTS (
                   SELECT 1 FROM credential_bindings
                   WHERE agent_id = NEW.agent_id AND status = 'active'
               ) THEN
                RAISE EXCEPTION 'first verified use must activate Agent and binding atomically'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_aggregate_from_grant() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_interaction_aggregate(OLD.enrollment_interaction_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_interaction_aggregate(NEW.enrollment_interaction_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_aggregate_from_interaction() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_interaction_aggregate(NEW.interaction_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_aggregate_from_name() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            target_interaction_id UUID;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                target_interaction_id := OLD.pending_interaction_id;
            ELSIF NEW.pending_interaction_id IS NOT NULL THEN
                target_interaction_id := NEW.pending_interaction_id;
            ELSE
                target_interaction_id := OLD.pending_interaction_id;
            END IF;
            IF target_interaction_id IS NOT NULL THEN
                PERFORM haku_0009_assert_interaction_aggregate(target_interaction_id);
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_binding_activation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_activation(NEW.binding_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_grant_consistency() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_grant_consistency(NEW.grant_id, TG_OP = 'INSERT');
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_name_promotion() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP <> 'DELETE' THEN
                PERFORM haku_0009_assert_name_promotion(NEW.reservation_id);
                RETURN NEW;
            END IF;
            RETURN OLD;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_new_interaction_correlation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM haku_0009_assert_correlation_reservation(NEW.interaction_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_principal_from_call() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_tool_call_principal(NEW.tool_call_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_principal_from_principal() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_tool_call_principal(OLD.tool_call_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_tool_call_principal(NEW.tool_call_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_subtype_from_binding() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_binding_subtype(OLD.binding_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_subtype(NEW.binding_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_subtype_from_grant() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_binding_subtype(OLD.binding_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_subtype(NEW.binding_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_check_subtype_from_static() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_binding_subtype(OLD.binding_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_subtype(NEW.binding_id);
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_client_software_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            redirect_uri TEXT;
        BEGIN
            FOREACH redirect_uri IN ARRAY NEW.validated_redirect_uris LOOP
                IF btrim(redirect_uri) = '' THEN
                    RAISE EXCEPTION 'validated redirect URIs must be non-empty'
                        USING ERRCODE = '23514';
                END IF;
            END LOOP;
            IF cardinality(NEW.validated_redirect_uris) <>
               (SELECT count(DISTINCT item) FROM unnest(NEW.validated_redirect_uris) AS item) THEN
                RAISE EXCEPTION 'validated redirect URIs must not contain duplicates'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' AND
               ROW(NEW.client_software_id, NEW.registration_kind, NEW.oauth_client_id, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.client_software_id, OLD.registration_kind, OLD.oauth_client_id, OLD.created_at) THEN
                RAISE EXCEPTION 'ClientSoftware registration identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_correlation_reservation_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF clock_timestamp() < OLD.release_after THEN
                    RAISE EXCEPTION 'correlation reservation cannot be released before its tombstone expiry'
                        USING ERRCODE = '23514';
                END IF;
                RETURN OLD;
            END IF;
            IF ROW(NEW.interaction_id, NEW.client_id, NEW.redirect_uri,
                   NEW.code_challenge, NEW.release_after)
               IS DISTINCT FROM
               ROW(OLD.interaction_id, OLD.client_id, OLD.redirect_uri,
                   OLD.code_challenge, OLD.release_after) THEN
                RAISE EXCEPTION 'correlation reservation is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_credential_binding_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            predecessor_agent_id UUID;
            predecessor_kind TEXT;
            predecessor_generation BIGINT;
        BEGIN
            PERFORM 1 FROM agents WHERE agent_id = NEW.agent_id FOR UPDATE;
            IF NEW.supersedes_binding_id IS NOT NULL THEN
                SELECT agent_id, kind::TEXT, generation
                INTO predecessor_agent_id, predecessor_kind, predecessor_generation
                FROM credential_bindings
                WHERE binding_id = NEW.supersedes_binding_id;
                IF NOT FOUND
                   OR predecessor_agent_id <> NEW.agent_id
                   OR predecessor_kind <> NEW.kind::TEXT
                   OR predecessor_generation >= NEW.generation THEN
                    RAISE EXCEPTION 'binding predecessor must be an older binding of the same Agent and kind'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            IF TG_OP = 'UPDATE' THEN
                IF ROW(NEW.binding_id, NEW.agent_id, NEW.kind, NEW.generation,
                       NEW.supersedes_binding_id, NEW.created_at)
                   IS DISTINCT FROM
                   ROW(OLD.binding_id, OLD.agent_id, OLD.kind, OLD.generation,
                       OLD.supersedes_binding_id, OLD.created_at) THEN
                    RAISE EXCEPTION 'CredentialBinding identity and lineage are immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.status <> OLD.status AND NOT (
                    (OLD.status = 'issuing' AND NEW.status IN ('issued', 'expired', 'failed'))
                    OR (OLD.status = 'issued' AND NEW.status IN ('active', 'revoked', 'expired', 'failed'))
                    OR (OLD.status = 'active' AND NEW.status IN ('revoked', 'expired'))
                ) THEN
                    RAISE EXCEPTION 'illegal CredentialBinding status transition: % -> %',
                        OLD.status, NEW.status USING ERRCODE = '23514';
                END IF;
                IF OLD.issued_at IS NOT NULL
                   AND NEW.issued_at IS DISTINCT FROM OLD.issued_at THEN
                    RAISE EXCEPTION 'binding issuance evidence is immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF OLD.activated_at IS NOT NULL
                   AND NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
                    RAISE EXCEPTION 'binding activation evidence is immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF OLD.ended_at IS NOT NULL
                   AND ROW(NEW.ended_at, NEW.end_reason)
                       IS DISTINCT FROM ROW(OLD.ended_at, OLD.end_reason) THEN
                    RAISE EXCEPTION 'binding terminal evidence is immutable'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_enrollment_interaction_delete_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF clock_timestamp() < OLD.correlation_release_after THEN
                RAISE EXCEPTION 'EnrollmentInteraction cannot be deleted before correlation release'
                    USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_enrollment_interaction_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            redirect_is_valid BOOLEAN;
        BEGIN
            SELECT NEW.redirect_uri = ANY(client.validated_redirect_uris)
            INTO redirect_is_valid
            FROM client_software AS client
            WHERE client.client_software_id = NEW.client_software_id
              AND client.oauth_client_id = NEW.client_id;
            IF redirect_is_valid IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'interaction redirect URI was not validated for its client software'
                    USING ERRCODE = '23514';
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NEW.phase <> 'awaiting_browser' THEN
                    RAISE EXCEPTION 'EnrollmentInteraction must begin in awaiting_browser'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(NEW.interaction_id, NEW.client_software_id, NEW.client_id,
                   NEW.redirect_uri, NEW.code_challenge, NEW.requested_scopes,
                   NEW.presentation_snapshot, NEW.upstream_authorization_url,
                   NEW.expires_at, NEW.correlation_release_after, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.interaction_id, OLD.client_software_id, OLD.client_id,
                   OLD.redirect_uri, OLD.code_challenge, OLD.requested_scopes,
                   OLD.presentation_snapshot, OLD.upstream_authorization_url,
                   OLD.expires_at, OLD.correlation_release_after, OLD.created_at) THEN
                RAISE EXCEPTION 'EnrollmentInteraction correlation and presentation are immutable'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.phase <> OLD.phase AND NOT (
                (OLD.phase = 'awaiting_browser' AND NEW.phase IN ('awaiting_approval', 'expired', 'failed'))
                OR (OLD.phase = 'awaiting_approval' AND NEW.phase IN ('allowed', 'denied', 'expired', 'failed'))
                OR (OLD.phase = 'allowed' AND NEW.phase IN ('exchanging', 'expired', 'failed'))
                OR (OLD.phase = 'exchanging' AND NEW.phase IN ('completed', 'expired', 'failed'))
            ) THEN
                RAISE EXCEPTION 'illegal EnrollmentInteraction phase transition: % -> %',
                    OLD.phase, NEW.phase USING ERRCODE = '23514';
            END IF;

            IF OLD.browser_nonce_digest IS NULL AND NEW.browser_nonce_digest IS NOT NULL THEN
                RAISE EXCEPTION 'consumed browser nonce cannot be restored'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.browser_nonce_digest IS NOT NULL
               AND NEW.browser_nonce_digest IS NOT NULL
               AND NEW.browser_nonce_digest IS DISTINCT FROM OLD.browser_nonce_digest THEN
                RAISE EXCEPTION 'browser nonce cannot be replaced'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.browser_identity_id IS NOT NULL
               AND NEW.browser_identity_id IS DISTINCT FROM OLD.browser_identity_id THEN
                RAISE EXCEPTION 'browser identity is immutable once established'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.browser_identity_id IS NULL AND NEW.browser_identity_id IS NOT NULL
               AND NOT (OLD.phase = 'awaiting_browser' AND NEW.phase = 'awaiting_approval') THEN
                RAISE EXCEPTION 'browser identity may only be established on browser arrival'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.browser_binding_digest IS NULL AND NEW.browser_binding_digest IS NOT NULL
               AND NOT (OLD.phase = 'awaiting_browser' AND NEW.phase = 'awaiting_approval') THEN
                RAISE EXCEPTION 'consumed browser binding cannot be restored'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.browser_binding_digest IS NOT NULL
               AND NEW.browser_binding_digest IS NOT NULL
               AND NEW.browser_binding_digest IS DISTINCT FROM OLD.browser_binding_digest THEN
                RAISE EXCEPTION 'browser binding cannot be replaced'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.browser_binding_digest IS NOT NULL AND NEW.browser_binding_digest IS NULL
               AND NEW.phase NOT IN ('completed', 'denied', 'expired', 'failed') THEN
                RAISE EXCEPTION 'browser binding may only be cleared in a terminal phase'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.decision_digest IS NOT NULL
               AND NEW.decision_digest IS DISTINCT FROM OLD.decision_digest THEN
                RAISE EXCEPTION 'enrollment decision is immutable once recorded'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.decision_digest IS NULL AND NEW.decision_digest IS NOT NULL
               AND NOT (OLD.phase = 'awaiting_approval' AND NEW.phase IN ('allowed', 'denied')) THEN
                RAISE EXCEPTION 'decision may only be recorded by an allow or deny transition'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.reconnect_agent_id IS NOT NULL AND
               ROW(NEW.reconnect_agent_id, NEW.reconnect_predecessor_binding_id)
               IS DISTINCT FROM
               ROW(OLD.reconnect_agent_id, OLD.reconnect_predecessor_binding_id) THEN
                RAISE EXCEPTION 'reconnect target is immutable once selected'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.reconnect_agent_id IS NULL AND NEW.reconnect_agent_id IS NOT NULL
               AND NOT (OLD.phase = 'awaiting_approval' AND NEW.phase = 'allowed') THEN
                RAISE EXCEPTION 'reconnect target may only be selected when allowing enrollment'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.closed_at IS NOT NULL AND
               ROW(NEW.closed_at, NEW.closure_reason)
               IS DISTINCT FROM ROW(OLD.closed_at, OLD.closure_reason) THEN
                RAISE EXCEPTION 'closed interaction metadata is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_identity_anchor_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF ROW(NEW.anchor_id, NEW.operator_id, NEW.trust_domain,
                   NEW.stable_external_user_key, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.anchor_id, OLD.operator_id, OLD.trust_domain,
                   OLD.stable_external_user_key, OLD.created_at) THEN
                RAISE EXCEPTION 'IdentityAnchor identity and Operator link are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_lock_grant_authority() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            target_operator UUID;
        BEGIN
            SELECT agent.owner_operator_id INTO target_operator
            FROM credential_bindings AS binding
            JOIN agents AS agent ON agent.agent_id = binding.agent_id
            WHERE binding.binding_id = NEW.binding_id
            FOR UPDATE OF agent;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'AuthorizationGrant binding must exist before grant creation'
                    USING ERRCODE = '23503';
            END IF;
            PERFORM 1 FROM operators WHERE operator_id = target_operator FOR UPDATE;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_oidc_identity_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF ROW(NEW.identity_id, NEW.anchor_id, NEW.issuer, NEW.subject, NEW.first_seen_at)
               IS DISTINCT FROM
               ROW(OLD.identity_id, OLD.anchor_id, OLD.issuer, OLD.subject, OLD.first_seen_at) THEN
                RAISE EXCEPTION 'OidcIdentity provenance and IdentityAnchor link are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_static_credential_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'StaticCredential rows are immutable; rotate the binding instead'
                USING ERRCODE = '23514';
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0009_tool_call_principal_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF ROW(NEW.tool_call_id, NEW.operator_id, NEW.binding_id)
               IS DISTINCT FROM ROW(OLD.tool_call_id, OLD.operator_id, OLD.binding_id) THEN
                RAISE EXCEPTION 'ToolCallPrincipal provenance is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0113_kubernetes_grant_source_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Shape is enforced independently by ck_kubernetes_grants_principal_shape.
    -- Let that constraint report malformed relational combinations instead of
    -- misclassifying them as source-provenance failures in this BEFORE trigger.
    IF NOT (
        (NEW.principal_kind = 'agent'
         AND NEW.principal_agent_id IS NOT NULL
         AND NEW.principal_agent_id = NEW.owner_agent_id
         AND NEW.principal_session_id IS NULL)
        OR
        (NEW.principal_kind = 'session'
         AND NEW.principal_agent_id IS NULL
         AND NEW.principal_session_id IS NOT NULL)
    ) THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.mcp_tool_calls AS call
        JOIN public.mcp_tool_call_principals AS request_principal
          ON request_principal.tool_call_id = call.tool_call_id
        JOIN public.credential_bindings AS binding
          ON binding.binding_id = request_principal.binding_id
        JOIN public.agents AS agent
          ON agent.agent_id = binding.agent_id
        WHERE call.tool_call_id = NEW.source_tool_call_id
          AND binding.agent_id = NEW.owner_agent_id
          AND agent.status NOT IN ('abandoned', 'deleted')
          AND call.server_id IN ('kubernetes', 'grants')
          AND call.tool_name = 'create_grant'
          AND call.status IN ('running', 'ok')
          AND call.approved_at IS NOT NULL
          AND call.approval_policy_id IS NULL
          AND (
            (NEW.principal_kind = 'agent'
             AND NEW.principal_agent_id = binding.agent_id)
            OR
            (NEW.principal_kind = 'session'
             AND request_principal.session_id IS NOT NULL
             AND NEW.principal_session_id = request_principal.session_id
             AND EXISTS (
               SELECT 1
               FROM public.sessions AS source_session
               WHERE source_session.session_id = request_principal.session_id
                 AND source_session.agent_binding_id = request_principal.binding_id
                 AND source_session.ended_at IS NULL
                 AND source_session.close_requested_at IS NULL
                 AND source_session.bridge_connected_at IS NOT NULL
                 AND source_session.lease_expires_at > statement_timestamp()
             ))
          )
    ) THEN
        RAISE EXCEPTION 'invalid Kubernetes grant source provenance or principal'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_kubernetes_grants_source_provenance';
    END IF;
    RETURN NEW;
END;
$$;
    """)

    op.execute(r"""
CREATE FUNCTION public.haku_0119_kubernetes_grant_source_invariants() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NOT (
        (NEW.principal_kind = 'agent'
         AND NEW.principal_access_profile_id IS NULL)
        OR
        (NEW.principal_kind = 'access_profile'
         AND NEW.principal_agent_id IS NULL
         AND NEW.principal_access_profile_id IS NOT NULL)
    ) THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.mcp_tool_calls AS call
        JOIN public.mcp_tool_call_principals AS request_principal
          ON request_principal.tool_call_id = call.tool_call_id
        JOIN public.credential_bindings AS binding
          ON binding.binding_id = request_principal.binding_id
        JOIN public.agents AS agent
          ON agent.agent_id = binding.agent_id
        WHERE call.tool_call_id = NEW.source_tool_call_id
          AND binding.agent_id = NEW.owner_agent_id
          AND agent.status NOT IN ('abandoned', 'deleted')
          AND call.server_id IN ('kubernetes', 'grants')
          AND call.tool_name = 'create_grant'
          AND call.status IN ('running', 'ok')
          AND call.approved_at IS NOT NULL
          AND call.approval_policy_id IS NULL
          AND (
            (NEW.principal_kind = 'agent'
             AND EXISTS (
               SELECT 1
               FROM public.agents AS target_agent
               WHERE target_agent.agent_id = NEW.principal_agent_id
                 AND target_agent.status NOT IN ('abandoned', 'deleted')
             ))
            OR
            (NEW.principal_kind = 'access_profile'
             AND NEW.principal_access_profile_id IS NOT NULL)
          )
    ) THEN
        RAISE EXCEPTION 'invalid Kubernetes grant source provenance or principal'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_kubernetes_grants_source_provenance';
    END IF;
    RETURN NEW;
END;
$$;
    """)

    op.execute(r"""
CREATE FUNCTION public.prevent_conversation_identity_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
                IF NEW.agent_id IS DISTINCT FROM OLD.agent_id
                   OR NEW.access_profile_id IS DISTINCT FROM OLD.access_profile_id
                   OR NEW.harness_kind IS DISTINCT FROM OLD.harness_kind THEN
                    RAISE EXCEPTION 'conversation identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$;
    """)


def _create_authority_tables() -> None:
    op.create_table(
        "agent_name_reservations",
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("display_name_key", sa.Text(), nullable=False),
        sa.Column("originating_interaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pending_interaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "((agent_id IS NULL) = (activated_at IS NULL))", name="ck_agent_name_reservations_activation_shape"
        ),
        sa.CheckConstraint("(char_length(display_name) <= 80)", name="ck_agent_name_reservations_display_name_length"),
        sa.CheckConstraint(
            "(display_name ~ '[^[:space:][:cntrl:]\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'::text)",
            name="ck_agent_name_reservations_display_name_nonempty",
        ),
        sa.CheckConstraint(
            "(num_nonnulls(pending_interaction_id, agent_id) = 1)", name="ck_agent_name_reservations_exactly_one_owner"
        ),
        sa.CheckConstraint(
            "(display_name_key ~ '[^[:space:][:cntrl:]\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'::text)",
            name="ck_agent_name_reservations_key_nonempty",
        ),
        sa.CheckConstraint(
            "((pending_interaction_id IS NULL) OR (originating_interaction_id = pending_interaction_id))",
            name="ck_agent_name_reservations_pending_origin",
        ),
        sa.PrimaryKeyConstraint("reservation_id", name="agent_name_reservations_pkey"),
        sa.UniqueConstraint("agent_id", "reservation_id", name="uq_agent_name_reservations_agent_reservation"),
        sa.UniqueConstraint("display_name_key", name="uq_agent_name_reservations_display_name_key"),
        sa.UniqueConstraint("pending_interaction_id", name="uq_agent_name_reservations_pending_interaction"),
    )

    op.create_table(
        "agents",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_operator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_name_reservation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", _AGENT_STATUS, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_approval_policy", sa.Text(), nullable=True),
        sa.Column("access_profile_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "((access_profile_id IS NULL) OR (btrim(access_profile_id) <> ''::text))",
            name="ck_agents_access_profile_id_nonempty",
        ),
        sa.CheckConstraint(
            "(((status = 'draft'::public.agent_status) AND (activated_at IS NULL)) OR ((status = 'abandoned'::public.agent_status) AND (activated_at IS NULL)) OR ((status = ANY (ARRAY['active'::public.agent_status, 'disabled'::public.agent_status, 'deleted'::public.agent_status])) AND (activated_at IS NOT NULL)))",
            name="ck_agents_status_shape",
        ),
        sa.PrimaryKeyConstraint("agent_id", name="agents_pkey"),
    )

    op.create_table(
        "authorization_grants",
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorizing_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_software_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_interaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("allowed_scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("initial_access_jti", sa.Text(), nullable=True),
        sa.Column("initial_refresh_jti", sa.Text(), nullable=True),
        sa.Column("token_family_persisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(array_position(allowed_scopes, NULL::text) IS NULL)",
            name="ck_authorization_grants_allowed_scopes_no_null",
        ),
        sa.CheckConstraint(
            "(((token_family_persisted_at IS NULL) AND (initial_access_jti IS NULL) AND (initial_refresh_jti IS NULL)) OR ((token_family_persisted_at IS NOT NULL) AND (initial_access_jti IS NOT NULL) AND (btrim(initial_access_jti) <> ''::text) AND ((initial_refresh_jti IS NULL) OR (btrim(initial_refresh_jti) <> ''::text))))",
            name="ck_authorization_grants_token_family_evidence_shape",
        ),
        sa.PrimaryKeyConstraint("grant_id", name="authorization_grants_pkey"),
        sa.UniqueConstraint("binding_id", name="uq_authorization_grants_binding_id"),
        sa.UniqueConstraint("enrollment_interaction_id", name="uq_authorization_grants_enrollment_interaction_id"),
    )

    op.create_table(
        "client_software",
        sa.Column("client_software_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("registration_kind", _CLIENT_REGISTRATION_KIND, nullable=False),
        sa.Column("oauth_client_id", sa.Text(), nullable=False),
        sa.Column("validated_redirect_uris", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("metadata_hash", sa.LargeBinary(), nullable=False),
        sa.Column("observed_name", sa.Text(), nullable=True),
        sa.Column("observed_icon_uri", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(octet_length(metadata_hash) > 0)", name="ck_client_software_metadata_hash_nonempty"),
        sa.CheckConstraint("(btrim(oauth_client_id) <> ''::text)", name="ck_client_software_oauth_client_id_nonempty"),
        sa.CheckConstraint(
            "(array_position(validated_redirect_uris, NULL::text) IS NULL)",
            name="ck_client_software_validated_redirect_uris_no_null",
        ),
        sa.CheckConstraint(
            "(cardinality(validated_redirect_uris) > 0)", name="ck_client_software_validated_redirect_uris_nonempty"
        ),
        sa.PrimaryKeyConstraint("client_software_id", name="client_software_pkey"),
        sa.UniqueConstraint("client_software_id", "oauth_client_id", name="uq_client_software_id_oauth_client_id"),
        sa.UniqueConstraint("oauth_client_id", name="uq_client_software_oauth_client_id"),
    )

    op.create_table(
        "credential_bindings",
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", _CREDENTIAL_KIND, nullable=False),
        sa.Column("status", _CREDENTIAL_BINDING_STATUS, nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, autoincrement=False),
        sa.Column("supersedes_binding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("(generation > 0)", name="ck_credential_bindings_generation_positive"),
        sa.CheckConstraint(
            "(((status = 'issuing'::public.credential_binding_status) AND (issued_at IS NULL) AND (activated_at IS NULL) AND (ended_at IS NULL) AND (end_reason IS NULL)) OR ((status = 'issued'::public.credential_binding_status) AND (issued_at IS NOT NULL) AND (activated_at IS NULL) AND (ended_at IS NULL) AND (end_reason IS NULL)) OR ((status = 'active'::public.credential_binding_status) AND (issued_at IS NOT NULL) AND (activated_at IS NOT NULL) AND (ended_at IS NULL) AND (end_reason IS NULL)) OR ((status = ANY (ARRAY['revoked'::public.credential_binding_status, 'expired'::public.credential_binding_status, 'failed'::public.credential_binding_status])) AND (ended_at IS NOT NULL) AND (end_reason IS NOT NULL) AND (btrim(end_reason) <> ''::text)))",
            name="ck_credential_bindings_status_shape",
        ),
        sa.CheckConstraint(
            "(((issued_at IS NULL) OR (issued_at >= created_at)) AND ((activated_at IS NULL) OR ((issued_at IS NOT NULL) AND (activated_at >= issued_at))) AND ((ended_at IS NULL) OR (ended_at >= COALESCE(activated_at, issued_at, created_at))))",
            name="ck_credential_bindings_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("binding_id", name="credential_bindings_pkey"),
        sa.UniqueConstraint("agent_id", "binding_id", name="uq_credential_bindings_agent_binding"),
        sa.UniqueConstraint("agent_id", "generation", name="uq_credential_bindings_agent_generation"),
    )

    op.create_table(
        "enrollment_correlation_reservations",
        sa.Column("interaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("release_after", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("interaction_id", name="enrollment_correlation_reservations_pkey"),
        sa.UniqueConstraint(
            "client_id", "redirect_uri", "code_challenge", name="uq_enrollment_correlation_reservations_tuple"
        ),
    )

    op.create_table(
        "enrollment_interactions",
        sa.Column("interaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_software_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("requested_scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("presentation_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("upstream_authorization_url", sa.Text(), nullable=False),
        sa.Column("phase", _ENROLLMENT_PHASE, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_release_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("browser_nonce_digest", sa.LargeBinary(), nullable=True),
        sa.Column("browser_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("browser_binding_digest", sa.LargeBinary(), nullable=True),
        sa.Column("decision_digest", sa.LargeBinary(), nullable=True),
        sa.Column("reconnect_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reconnect_predecessor_binding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("closure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_approval_policy", sa.Text(), nullable=True),
        sa.Column("access_profile_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "((access_profile_id IS NULL) OR (btrim(access_profile_id) <> ''::text))",
            name="ck_enrollment_interactions_access_profile_id_nonempty",
        ),
        sa.CheckConstraint(
            "((browser_binding_digest IS NULL) OR (browser_identity_id IS NOT NULL))",
            name="ck_enrollment_interactions_browser_binding_shape",
        ),
        sa.CheckConstraint("(btrim(client_id) <> ''::text)", name="ck_enrollment_interactions_client_id_nonempty"),
        sa.CheckConstraint(
            "(btrim(code_challenge) <> ''::text)", name="ck_enrollment_interactions_code_challenge_nonempty"
        ),
        sa.CheckConstraint(
            "(correlation_release_after > expires_at)",
            name="ck_enrollment_interactions_correlation_outlives_interaction",
        ),
        sa.CheckConstraint(
            "(((phase = 'awaiting_browser'::public.enrollment_phase) AND (browser_nonce_digest IS NOT NULL) AND (browser_identity_id IS NULL) AND (decision_digest IS NULL) AND (reconnect_agent_id IS NULL) AND (closed_at IS NULL) AND (closure_reason IS NULL)) OR ((phase = 'awaiting_approval'::public.enrollment_phase) AND (browser_nonce_digest IS NULL) AND (browser_identity_id IS NOT NULL) AND (browser_binding_digest IS NOT NULL) AND (decision_digest IS NULL) AND (reconnect_agent_id IS NULL) AND (closed_at IS NULL) AND (closure_reason IS NULL)) OR ((phase = ANY (ARRAY['allowed'::public.enrollment_phase, 'exchanging'::public.enrollment_phase])) AND (browser_nonce_digest IS NULL) AND (browser_identity_id IS NOT NULL) AND (browser_binding_digest IS NOT NULL) AND (decision_digest IS NOT NULL) AND (closed_at IS NULL) AND (closure_reason IS NULL)) OR ((phase = 'completed'::public.enrollment_phase) AND (browser_nonce_digest IS NULL) AND (browser_identity_id IS NOT NULL) AND (browser_binding_digest IS NULL) AND (decision_digest IS NOT NULL) AND (closed_at IS NOT NULL) AND (closure_reason IS NOT NULL) AND (btrim(closure_reason) <> ''::text)) OR ((phase = 'denied'::public.enrollment_phase) AND (browser_nonce_digest IS NULL) AND (browser_identity_id IS NOT NULL) AND (browser_binding_digest IS NULL) AND (decision_digest IS NOT NULL) AND (reconnect_agent_id IS NULL) AND (closed_at IS NOT NULL) AND (closure_reason IS NOT NULL) AND (btrim(closure_reason) <> ''::text)) OR ((phase = ANY (ARRAY['expired'::public.enrollment_phase, 'failed'::public.enrollment_phase])) AND (browser_nonce_digest IS NULL) AND (browser_binding_digest IS NULL) AND (closed_at IS NOT NULL) AND (closure_reason IS NOT NULL) AND (btrim(closure_reason) <> ''::text)))",
            name="ck_enrollment_interactions_phase_shape",
        ),
        sa.CheckConstraint(
            "((reconnect_agent_id IS NULL) = (reconnect_predecessor_binding_id IS NULL))",
            name="ck_enrollment_interactions_reconnect_shape",
        ),
        sa.CheckConstraint(
            "(btrim(redirect_uri) <> ''::text)", name="ck_enrollment_interactions_redirect_uri_nonempty"
        ),
        sa.CheckConstraint(
            "(array_position(requested_scopes, NULL::text) IS NULL)",
            name="ck_enrollment_interactions_requested_scopes_no_null",
        ),
        sa.CheckConstraint(
            "(btrim(upstream_authorization_url) <> ''::text)", name="ck_enrollment_interactions_upstream_url_nonempty"
        ),
        sa.PrimaryKeyConstraint("interaction_id", name="enrollment_interactions_pkey"),
        sa.UniqueConstraint(
            "interaction_id",
            "client_id",
            "redirect_uri",
            "code_challenge",
            "correlation_release_after",
            name="uq_enrollment_interactions_correlation_component",
        ),
    )

    op.create_table(
        "identity_anchors",
        sa.Column("anchor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trust_domain", sa.Text(), nullable=False),
        sa.Column("stable_external_user_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(btrim(stable_external_user_key) <> ''::text)", name="ck_identity_anchors_external_key_nonempty"
        ),
        sa.CheckConstraint("(btrim(trust_domain) <> ''::text)", name="ck_identity_anchors_trust_domain_nonempty"),
        sa.PrimaryKeyConstraint("anchor_id", name="identity_anchors_pkey"),
        sa.UniqueConstraint(
            "trust_domain", "stable_external_user_key", name="uq_identity_anchors_trust_domain_external_key"
        ),
    )

    op.create_table(
        "oidc_identities",
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anchor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(btrim(issuer) <> ''::text)", name="ck_oidc_identities_issuer_nonempty"),
        sa.CheckConstraint("(btrim(subject) <> ''::text)", name="ck_oidc_identities_subject_nonempty"),
        sa.PrimaryKeyConstraint("identity_id", name="oidc_identities_pkey"),
        sa.UniqueConstraint("issuer", "subject", name="uq_oidc_identities_issuer_subject"),
    )

    op.create_table(
        "operators",
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", _OPERATOR_STATUS, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("operator_id", name="operators_pkey"),
    )

    op.create_table(
        "static_credentials",
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("secret_reference", sa.Text(), nullable=False),
        sa.Column("credential_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(octet_length(credential_fingerprint) > 0)", name="ck_static_credentials_fingerprint_nonempty"
        ),
        sa.CheckConstraint(
            "(btrim(secret_reference) <> ''::text)", name="ck_static_credentials_secret_reference_nonempty"
        ),
        sa.PrimaryKeyConstraint("binding_id", name="static_credentials_pkey"),
        sa.UniqueConstraint("credential_fingerprint", name="uq_static_credentials_fingerprint"),
    )


def _create_console_tables() -> None:
    op.create_table(
        "kubernetes_grants",
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_tool_call_id", sa.Text(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("principal_kind", sa.Text(), nullable=False),
        sa.Column("principal_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("principal_access_profile_id", sa.Text(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(((ended_at IS NOT NULL) OR (end_reason IS NULL)) AND ((end_reason IS NULL) OR (btrim(end_reason) <> ''::text)))",
            name="ck_kubernetes_grants_end_shape",
        ),
        sa.CheckConstraint(
            "((expires_at IS NULL) OR (expires_at > created_at))", name="ck_kubernetes_grants_expiration_after_creation"
        ),
        sa.CheckConstraint(
            "(((principal_kind = 'agent'::text) AND (principal_agent_id IS NOT NULL) AND (principal_access_profile_id IS NULL)) OR ((principal_kind = 'access_profile'::text) AND (principal_agent_id IS NULL) AND (principal_access_profile_id IS NOT NULL)))",
            name="ck_kubernetes_grants_principal_shape",
        ),
        sa.CheckConstraint(
            "((jsonb_typeof(rules) = 'array'::text) AND (jsonb_array_length(rules) > 0))",
            name="ck_kubernetes_grants_rules_nonempty",
        ),
        sa.CheckConstraint(
            "((jsonb_typeof(scope) = 'object'::text) AND (scope ? 'kind'::text) AND ((scope ->> 'kind'::text) = ANY (ARRAY['namespaces'::text, 'all_namespaces'::text, 'cluster'::text, 'non_resource'::text])) AND ((((scope ->> 'kind'::text) = 'namespaces'::text) AND (scope ? 'namespaces'::text) AND (jsonb_typeof((scope -> 'namespaces'::text)) = 'array'::text) AND (jsonb_array_length((scope -> 'namespaces'::text)) > 0)) OR (((scope ->> 'kind'::text) <> 'namespaces'::text) AND (NOT (scope ? 'namespaces'::text)))))",
            name="ck_kubernetes_grants_scope_shape",
        ),
        sa.CheckConstraint(
            "(btrim(source_tool_call_id) <> ''::text)", name="ck_kubernetes_grants_source_tool_call_nonempty"
        ),
        sa.PrimaryKeyConstraint("grant_id", name="kubernetes_grants_pkey"),
    )

    op.create_table(
        "mcp_tool_call_principals",
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(num_nonnulls(operator_id, binding_id) = 1)", name="ck_mcp_tool_call_principals_exactly_one_variant"
        ),
        sa.CheckConstraint(
            "((session_id IS NULL) OR (binding_id IS NOT NULL))", name="ck_mcp_tool_call_principals_session_agent"
        ),
        sa.PrimaryKeyConstraint("tool_call_id", name="mcp_tool_call_principals_pkey"),
    )

    op.create_table(
        "mcp_tool_calls",
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("status", _TOOL_CALL_STATUS, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arguments_json", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approval_policy_id", sa.Text(), nullable=True),
        sa.Column("auto_approval_evaluation", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawal_reason", sa.Text(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decision_operator_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "((decision_note IS NULL) OR (char_length(decision_note) <= 4096))",
            name="ck_mcp_tool_calls_decision_note_length",
        ),
        sa.PrimaryKeyConstraint("tool_call_id", name="mcp_tool_calls_pkey"),
    )

    op.create_table(
        "operator_login_flows",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("browser_binding", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(btrim(browser_binding) <> ''::text)", name="ck_operator_login_flows_browser_binding_nonempty"
        ),
        sa.PrimaryKeyConstraint("state", name="operator_login_flows_pkey"),
    )

    op.create_table(
        "push_subscriptions",
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("endpoint", name="push_subscriptions_pkey"),
    )


def _create_recall_tables() -> None:
    op.create_table(
        "content_embeddings",
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("embedding", HalfVector(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("content_sha", "model_key", name="content_embeddings_pkey"),
        schema="recall_index",
    )

    op.create_table(
        "contents",
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("content_sha", name="contents_pkey"),
        schema="recall_index",
    )

    op.create_table(
        "git_chunks",
        sa.Column("blob_sha", sa.Text(), nullable=False),
        sa.Column("chunker_key", sa.Text(), nullable=False),
        sa.Column("byte_start", sa.BigInteger(), nullable=False, autoincrement=False),
        sa.Column("byte_end", sa.BigInteger(), nullable=False, autoincrement=False),
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("index_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("index_id", "blob_sha", "chunker_key", "byte_start", name="git_chunks_pkey"),
        schema="recall_index",
    )

    op.create_table(
        "git_sync_state",
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("remote_commit", sa.Text(), nullable=True),
        sa.Column("remote_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("chunker_key", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("index_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(((commit_sha IS NULL) = (chunker_key IS NULL)) AND ((commit_sha IS NULL) = (synced_at IS NULL)))",
            name="ck_git_sync_state_indexed_half",
        ),
        sa.PrimaryKeyConstraint("index_id", name="git_sync_state_pkey"),
        schema="recall_index",
    )

    op.create_table(
        "git_tip",
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.Text(), nullable=False),
        sa.Column("index_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("index_id", "path", name="git_tip_pkey"),
        schema="recall_index",
    )

    op.create_table(
        "indexes",
        sa.Column("index_id", sa.Text(), nullable=False),
        sa.Column("index_type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("(index_type = 'git'::text)", name="ck_indexes_index_type"),
        sa.PrimaryKeyConstraint("index_id", name="indexes_pkey"),
        schema="recall_index",
    )


def _preserve_deployed_constraint_names() -> None:
    # PostgreSQL kept these names through column renames; reproduce them on fresh databases.
    op.execute(
        "ALTER TABLE public.kubernetes_grants RENAME CONSTRAINT kubernetes_grants_owner_agent_id_not_null TO kubernetes_grants_agent_id_not_null"
    )


def _create_foreign_keys() -> None:
    # Added after tables because the authority/reference graph contains cycles.
    op.create_foreign_key(
        "agent_name_reservations_originating_interaction_id_fkey",
        "agent_name_reservations",
        "enrollment_interactions",
        ["originating_interaction_id"],
        ["interaction_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "agent_name_reservations_pending_interaction_id_fkey",
        "agent_name_reservations",
        "enrollment_interactions",
        ["pending_interaction_id"],
        ["interaction_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "agents_owner_operator_id_fkey",
        "agents",
        "operators",
        ["owner_operator_id"],
        ["operator_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "authorization_grants_authorizing_identity_id_fkey",
        "authorization_grants",
        "oidc_identities",
        ["authorizing_identity_id"],
        ["identity_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "authorization_grants_client_software_id_fkey",
        "authorization_grants",
        "client_software",
        ["client_software_id"],
        ["client_software_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "authorization_grants_enrollment_interaction_id_fkey",
        "authorization_grants",
        "enrollment_interactions",
        ["enrollment_interaction_id"],
        ["interaction_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "credential_bindings_agent_id_fkey",
        "credential_bindings",
        "agents",
        ["agent_id"],
        ["agent_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "enrollment_interactions_browser_identity_id_fkey",
        "enrollment_interactions",
        "oidc_identities",
        ["browser_identity_id"],
        ["identity_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "fk_agent_name_reservations_agent",
        "agent_name_reservations",
        "agents",
        ["agent_id"],
        ["agent_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_foreign_key(
        "fk_agents_owned_current_name",
        "agents",
        "agent_name_reservations",
        ["agent_id", "current_name_reservation_id"],
        ["agent_id", "reservation_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_foreign_key(
        "fk_authorization_grants_binding",
        "authorization_grants",
        "credential_bindings",
        ["binding_id"],
        ["binding_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_foreign_key(
        "fk_credential_bindings_same_agent_predecessor",
        "credential_bindings",
        "credential_bindings",
        ["agent_id", "supersedes_binding_id"],
        ["agent_id", "binding_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_foreign_key(
        "fk_enrollment_correlation_reservations_exact_interaction",
        "enrollment_correlation_reservations",
        "enrollment_interactions",
        ["interaction_id", "client_id", "redirect_uri", "code_challenge", "release_after"],
        ["interaction_id", "client_id", "redirect_uri", "code_challenge", "correlation_release_after"],
        ondelete="CASCADE",
    )

    op.create_foreign_key(
        "fk_enrollment_interactions_exact_client_software",
        "enrollment_interactions",
        "client_software",
        ["client_software_id", "client_id"],
        ["client_software_id", "oauth_client_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "fk_enrollment_interactions_reconnect_predecessor",
        "enrollment_interactions",
        "credential_bindings",
        ["reconnect_agent_id", "reconnect_predecessor_binding_id"],
        ["agent_id", "binding_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_foreign_key(
        "fk_mcp_tool_calls_decision_operator",
        "mcp_tool_calls",
        "operators",
        ["decision_operator_id"],
        ["operator_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "fk_static_credentials_binding",
        "static_credentials",
        "credential_bindings",
        ["binding_id"],
        ["binding_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_foreign_key(
        "identity_anchors_operator_id_fkey",
        "identity_anchors",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "kubernetes_grants_owner_agent_id_fkey",
        "kubernetes_grants",
        "agents",
        ["owner_agent_id"],
        ["agent_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "kubernetes_grants_principal_agent_id_fkey",
        "kubernetes_grants",
        "agents",
        ["principal_agent_id"],
        ["agent_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "kubernetes_grants_source_tool_call_id_fkey",
        "kubernetes_grants",
        "mcp_tool_calls",
        ["source_tool_call_id"],
        ["tool_call_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "mcp_tool_call_principals_binding_id_fkey",
        "mcp_tool_call_principals",
        "credential_bindings",
        ["binding_id"],
        ["binding_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "mcp_tool_call_principals_operator_id_fkey",
        "mcp_tool_call_principals",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "mcp_tool_call_principals_tool_call_id_fkey",
        "mcp_tool_call_principals",
        "mcp_tool_calls",
        ["tool_call_id"],
        ["tool_call_id"],
        ondelete="CASCADE",
    )

    op.create_foreign_key(
        "oidc_identities_anchor_id_fkey",
        "oidc_identities",
        "identity_anchors",
        ["anchor_id"],
        ["anchor_id"],
        ondelete="RESTRICT",
    )

    op.create_foreign_key(
        "push_subscriptions_operator_id_fkey",
        "push_subscriptions",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="CASCADE",
    )

    op.create_foreign_key(
        "content_embeddings_content_sha_fkey",
        "content_embeddings",
        "contents",
        ["content_sha"],
        ["content_sha"],
        source_schema="recall_index",
        referent_schema="recall_index",
    )

    op.create_foreign_key(
        "git_chunks_content_sha_fkey",
        "git_chunks",
        "contents",
        ["content_sha"],
        ["content_sha"],
        source_schema="recall_index",
        referent_schema="recall_index",
    )

    op.create_foreign_key(
        "git_chunks_index_id_fkey",
        "git_chunks",
        "indexes",
        ["index_id"],
        ["index_id"],
        source_schema="recall_index",
        referent_schema="recall_index",
    )

    op.create_foreign_key(
        "git_sync_state_index_id_fkey",
        "git_sync_state",
        "indexes",
        ["index_id"],
        ["index_id"],
        source_schema="recall_index",
        referent_schema="recall_index",
    )

    op.create_foreign_key(
        "git_tip_index_id_fkey",
        "git_tip",
        "indexes",
        ["index_id"],
        ["index_id"],
        source_schema="recall_index",
        referent_schema="recall_index",
    )


def _create_indexes() -> None:
    op.create_index("idx_agent_name_reservations_agent_id", "agent_name_reservations", ["agent_id"])

    op.create_index("idx_agents_owner_operator_id", "agents", ["owner_operator_id"])

    op.create_index(
        "idx_authorization_grants_authorizing_identity_id", "authorization_grants", ["authorizing_identity_id"]
    )

    op.create_index("idx_authorization_grants_client_software_id", "authorization_grants", ["client_software_id"])

    op.create_index("idx_credential_bindings_agent_id", "credential_bindings", ["agent_id"])

    op.create_index("idx_enrollment_interactions_client_software_id", "enrollment_interactions", ["client_software_id"])

    op.create_index("idx_enrollment_interactions_phase_expires_at", "enrollment_interactions", ["phase", "expires_at"])

    op.create_index("idx_identity_anchors_operator_id", "identity_anchors", ["operator_id"])

    op.create_index(
        "idx_kubernetes_grants_access_profile_principal_expiry",
        "kubernetes_grants",
        ["principal_access_profile_id", "expires_at"],
    )

    op.create_index(
        "idx_kubernetes_grants_agent_principal_expiry", "kubernetes_grants", ["principal_agent_id", "expires_at"]
    )

    op.create_index("idx_kubernetes_grants_owner_expiry", "kubernetes_grants", ["owner_agent_id", "expires_at"])

    op.create_index("idx_kubernetes_grants_source_tool_call", "kubernetes_grants", ["source_tool_call_id"])

    op.create_index("idx_mcp_tool_call_principals_binding_id", "mcp_tool_call_principals", ["binding_id"])

    op.create_index("idx_mcp_tool_call_principals_operator_id", "mcp_tool_call_principals", ["operator_id"])

    op.create_index("idx_mcp_tool_call_principals_session_id", "mcp_tool_call_principals", ["session_id"])

    op.create_index("idx_mcp_tool_calls_created_at", "mcp_tool_calls", ["created_at"])

    op.create_index("idx_oidc_identities_anchor_id", "oidc_identities", ["anchor_id"])

    op.create_index("idx_operator_login_flows_expires_at", "operator_login_flows", ["expires_at"])

    op.create_index("idx_push_subscriptions_operator_id", "push_subscriptions", ["operator_id"])

    op.create_index(
        "uq_credential_bindings_one_active_per_agent",
        "credential_bindings",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("(status = 'active'::public.credential_binding_status)"),
    )


def _create_triggers() -> None:
    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_agent_active_bindings AFTER INSERT OR UPDATE ON public.agents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_agent_active_bindings()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_binding_activation AFTER INSERT OR UPDATE ON public.credential_bindings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_binding_activation()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_binding_subtype AFTER INSERT OR DELETE OR UPDATE ON public.credential_bindings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_subtype_from_binding()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_call_has_principal AFTER INSERT OR UPDATE ON public.mcp_tool_calls DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_principal_from_call()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_grant_consistency AFTER INSERT OR UPDATE ON public.authorization_grants DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_grant_consistency()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_grant_interaction_aggregate AFTER INSERT OR DELETE OR UPDATE ON public.authorization_grants DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_aggregate_from_grant()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_grant_subtype AFTER INSERT OR DELETE OR UPDATE ON public.authorization_grants DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_subtype_from_grant()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_interaction_aggregate AFTER INSERT OR UPDATE ON public.enrollment_interactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_aggregate_from_interaction()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_name_interaction_aggregate AFTER INSERT OR DELETE OR UPDATE ON public.agent_name_reservations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_aggregate_from_name()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_name_promotion AFTER INSERT OR UPDATE ON public.agent_name_reservations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_name_promotion()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_new_interaction_has_correlation AFTER INSERT ON public.enrollment_interactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_new_interaction_correlation()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_principal_has_call AFTER INSERT OR DELETE OR UPDATE ON public.mcp_tool_call_principals DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_principal_from_principal()"
    )

    op.execute(
        "CREATE CONSTRAINT TRIGGER ctrg_haku_0009_static_subtype AFTER INSERT OR DELETE OR UPDATE ON public.static_credentials DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.haku_0009_check_subtype_from_static()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_agent_invariants BEFORE INSERT OR DELETE OR UPDATE ON public.agents FOR EACH ROW EXECUTE FUNCTION public.haku_0009_agent_invariants()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_agent_name_invariants BEFORE INSERT OR DELETE OR UPDATE ON public.agent_name_reservations FOR EACH ROW EXECUTE FUNCTION public.haku_0009_agent_name_invariants()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_authorization_grant_immutable BEFORE UPDATE ON public.authorization_grants FOR EACH ROW EXECUTE FUNCTION public.haku_0009_authorization_grant_immutable()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_client_software_invariants BEFORE INSERT OR UPDATE ON public.client_software FOR EACH ROW EXECUTE FUNCTION public.haku_0009_client_software_invariants()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_correlation_reservation_invariants BEFORE DELETE OR UPDATE ON public.enrollment_correlation_reservations FOR EACH ROW EXECUTE FUNCTION public.haku_0009_correlation_reservation_invariants()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_credential_binding_invariants BEFORE INSERT OR UPDATE ON public.credential_bindings FOR EACH ROW EXECUTE FUNCTION public.haku_0009_credential_binding_invariants()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_enrollment_interaction_delete_guard BEFORE DELETE ON public.enrollment_interactions FOR EACH ROW EXECUTE FUNCTION public.haku_0009_enrollment_interaction_delete_guard()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_enrollment_interaction_invariants BEFORE INSERT OR UPDATE ON public.enrollment_interactions FOR EACH ROW EXECUTE FUNCTION public.haku_0009_enrollment_interaction_invariants()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_identity_anchor_immutable BEFORE UPDATE ON public.identity_anchors FOR EACH ROW EXECUTE FUNCTION public.haku_0009_identity_anchor_immutable()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_lock_grant_authority BEFORE INSERT OR UPDATE ON public.authorization_grants FOR EACH ROW EXECUTE FUNCTION public.haku_0009_lock_grant_authority()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_oidc_identity_immutable BEFORE UPDATE ON public.oidc_identities FOR EACH ROW EXECUTE FUNCTION public.haku_0009_oidc_identity_immutable()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_static_credential_immutable BEFORE UPDATE ON public.static_credentials FOR EACH ROW EXECUTE FUNCTION public.haku_0009_static_credential_immutable()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0009_tool_call_principal_immutable BEFORE UPDATE ON public.mcp_tool_call_principals FOR EACH ROW EXECUTE FUNCTION public.haku_0009_tool_call_principal_immutable()"
    )

    op.execute(
        "CREATE TRIGGER trg_haku_0119_kubernetes_grant_source_invariants BEFORE INSERT OR UPDATE OF owner_agent_id, principal_kind, principal_agent_id, principal_access_profile_id, source_tool_call_id ON public.kubernetes_grants FOR EACH ROW EXECUTE FUNCTION public.haku_0119_kubernetes_grant_source_invariants()"
    )
