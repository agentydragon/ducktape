"""Create the canonical Haku console schema.

Revision ID: 0010
Revises: None

Revision ``0010`` was retained when the deployed migration chain was squashed. Databases already
stamped at 0010 therefore remain at head, while fresh databases create this frozen schema directly.
The deployed ``haku_0009_*`` function and trigger names remain unchanged so fresh and existing
databases have the same physical schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB, UUID

revision: str = "0010"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOOL_CALL_STATUS_VALUES = ("pending_approval", "running", "ok", "error", "denied")
_OPERATOR_STATUSES = ("active", "disabled")
_CLIENT_REGISTRATION_KINDS = ("oauth_proxy_unclassified", "dcr", "cimd", "preregistered")
_ENROLLMENT_PHASES = (
    "awaiting_browser",
    "awaiting_approval",
    "allowed",
    "exchanging",
    "completed",
    "denied",
    "expired",
    "failed",
)
_AGENT_STATUSES = ("draft", "active", "abandoned", "disabled", "deleted")
_CREDENTIAL_KINDS = ("oauth", "static")
_CREDENTIAL_BINDING_STATUSES = ("issuing", "issued", "active", "revoked", "expired", "failed")


def _tool_call_status_enum(*, create_type: bool = False) -> ENUM:
    return ENUM(*_TOOL_CALL_STATUS_VALUES, name="tool_call_status", create_type=create_type)


def _operator_status_enum(*, create_type: bool = False) -> ENUM:
    return ENUM(*_OPERATOR_STATUSES, name="operator_status", create_type=create_type)


def _client_registration_kind(*, create_type: bool = False) -> ENUM:
    return ENUM(*_CLIENT_REGISTRATION_KINDS, name="client_registration_kind", create_type=create_type)


def _enrollment_phase(*, create_type: bool = False) -> ENUM:
    return ENUM(*_ENROLLMENT_PHASES, name="enrollment_phase", create_type=create_type)


def _agent_status(*, create_type: bool = False) -> ENUM:
    return ENUM(*_AGENT_STATUSES, name="agent_status", create_type=create_type)


def _credential_kind(*, create_type: bool = False) -> ENUM:
    return ENUM(*_CREDENTIAL_KINDS, name="credential_kind", create_type=create_type)


def _credential_binding_status(*, create_type: bool = False) -> ENUM:
    return ENUM(*_CREDENTIAL_BINDING_STATUSES, name="credential_binding_status", create_type=create_type)


def _create_core_tables() -> None:
    op.create_table(
        "operators",
        sa.Column("operator_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("status", _operator_status_enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "identity_anchors",
        sa.Column("anchor_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operator_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operators.operator_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trust_domain", sa.Text(), nullable=False),
        sa.Column("stable_external_user_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(trust_domain) <> ''", name="ck_identity_anchors_trust_domain_nonempty"),
        sa.CheckConstraint("btrim(stable_external_user_key) <> ''", name="ck_identity_anchors_external_key_nonempty"),
        sa.UniqueConstraint(
            "trust_domain", "stable_external_user_key", name="uq_identity_anchors_trust_domain_external_key"
        ),
    )
    op.create_index("idx_identity_anchors_operator_id", "identity_anchors", ["operator_id"])
    op.create_table(
        "oidc_identities",
        sa.Column("identity_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "anchor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("identity_anchors.anchor_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(issuer) <> ''", name="ck_oidc_identities_issuer_nonempty"),
        sa.CheckConstraint("btrim(subject) <> ''", name="ck_oidc_identities_subject_nonempty"),
        sa.UniqueConstraint("issuer", "subject", name="uq_oidc_identities_issuer_subject"),
    )
    op.create_index("idx_oidc_identities_anchor_id", "oidc_identities", ["anchor_id"])

    # Column order matches the deployed catalog after the old chain's additive changes and
    # cutovers. Keeping it stable makes schema dumps useful for exact baseline verification.
    op.create_table(
        "mcp_operator_oauth_associations",
        sa.Column("server_id", sa.Text(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("client_secret_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.Text(), nullable=True),
        sa.Column("token_endpoint", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("association_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_revision", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operators.operator_id"],
            name="fk_mcp_operator_oauth_associations_operator",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("association_id", name="uq_mcp_operator_oauth_associations_association_id"),
    )
    op.create_index("idx_mcp_operator_oauth_associations_operator", "mcp_operator_oauth_associations", ["operator_id"])
    op.create_table(
        "mcp_operator_oauth_flows",
        sa.Column("state", sa.Text(), primary_key=True),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("client_secret_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.Text(), nullable=True),
        sa.Column("token_endpoint", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="fk_mcp_operator_oauth_flows_operator", ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_mcp_operator_oauth_flows_server_operator", "mcp_operator_oauth_flows", ["server_id", "operator_id"]
    )
    op.create_index("idx_mcp_operator_oauth_flows_expires_at", "mcp_operator_oauth_flows", ["expires_at"])

    op.create_table(
        "mcp_tool_calls",
        sa.Column("tool_call_id", sa.Text(), primary_key=True),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("status", _tool_call_status_enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arguments_json", JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("result_json", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column("approval_policy_id", sa.Text(), nullable=True),
        sa.Column("auto_approval_evaluation", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_mcp_tool_calls_created_at", "mcp_tool_calls", ["created_at"])


def _create_authority_tables() -> None:
    op.create_table(
        "client_software",
        sa.Column("client_software_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("registration_kind", _client_registration_kind(), nullable=False),
        sa.Column("oauth_client_id", sa.Text(), nullable=False),
        sa.Column("validated_redirect_uris", ARRAY(sa.Text()), nullable=False),
        sa.Column("metadata_hash", sa.LargeBinary(), nullable=False),
        sa.Column("observed_name", sa.Text(), nullable=True),
        sa.Column("observed_icon_uri", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(oauth_client_id) <> ''", name="ck_client_software_oauth_client_id_nonempty"),
        sa.CheckConstraint(
            "cardinality(validated_redirect_uris) > 0", name="ck_client_software_validated_redirect_uris_nonempty"
        ),
        sa.CheckConstraint(
            "array_position(validated_redirect_uris, NULL) IS NULL",
            name="ck_client_software_validated_redirect_uris_no_null",
        ),
        sa.CheckConstraint("octet_length(metadata_hash) > 0", name="ck_client_software_metadata_hash_nonempty"),
        sa.UniqueConstraint("oauth_client_id", name="uq_client_software_oauth_client_id"),
        sa.UniqueConstraint("client_software_id", "oauth_client_id", name="uq_client_software_id_oauth_client_id"),
    )

    op.create_table(
        "enrollment_interactions",
        sa.Column("interaction_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_software_id", UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("requested_scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column("presentation_snapshot", JSONB(), nullable=False),
        sa.Column("upstream_authorization_url", sa.Text(), nullable=False),
        sa.Column("phase", _enrollment_phase(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_release_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("browser_nonce_digest", sa.LargeBinary(), nullable=True),
        sa.Column(
            "browser_identity_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oidc_identities.identity_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("browser_binding_digest", sa.LargeBinary(), nullable=True),
        sa.Column("decision_digest", sa.LargeBinary(), nullable=True),
        sa.Column("reconnect_agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("reconnect_predecessor_binding_id", UUID(as_uuid=True), nullable=True),
        sa.Column("closure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(client_id) <> ''", name="ck_enrollment_interactions_client_id_nonempty"),
        sa.CheckConstraint("btrim(redirect_uri) <> ''", name="ck_enrollment_interactions_redirect_uri_nonempty"),
        sa.CheckConstraint("btrim(code_challenge) <> ''", name="ck_enrollment_interactions_code_challenge_nonempty"),
        sa.CheckConstraint(
            "btrim(upstream_authorization_url) <> ''", name="ck_enrollment_interactions_upstream_url_nonempty"
        ),
        sa.CheckConstraint(
            "array_position(requested_scopes, NULL) IS NULL", name="ck_enrollment_interactions_requested_scopes_no_null"
        ),
        sa.CheckConstraint(
            "correlation_release_after > expires_at", name="ck_enrollment_interactions_correlation_outlives_interaction"
        ),
        sa.CheckConstraint(
            "browser_binding_digest IS NULL OR browser_identity_id IS NOT NULL",
            name="ck_enrollment_interactions_browser_binding_shape",
        ),
        sa.CheckConstraint(
            "(reconnect_agent_id IS NULL) = (reconnect_predecessor_binding_id IS NULL)",
            name="ck_enrollment_interactions_reconnect_shape",
        ),
        sa.CheckConstraint(
            """
            (
                phase = 'awaiting_browser'
                AND browser_nonce_digest IS NOT NULL
                AND browser_identity_id IS NULL
                AND decision_digest IS NULL
                AND reconnect_agent_id IS NULL
                AND closed_at IS NULL
                AND closure_reason IS NULL
            ) OR (
                phase = 'awaiting_approval'
                AND browser_nonce_digest IS NULL
                AND browser_identity_id IS NOT NULL
                AND browser_binding_digest IS NOT NULL
                AND decision_digest IS NULL
                AND reconnect_agent_id IS NULL
                AND closed_at IS NULL
                AND closure_reason IS NULL
            ) OR (
                phase IN ('allowed', 'exchanging')
                AND browser_nonce_digest IS NULL
                AND browser_identity_id IS NOT NULL
                AND browser_binding_digest IS NOT NULL
                AND decision_digest IS NOT NULL
                AND closed_at IS NULL
                AND closure_reason IS NULL
            ) OR (
                phase = 'completed'
                AND browser_nonce_digest IS NULL
                AND browser_identity_id IS NOT NULL
                AND browser_binding_digest IS NULL
                AND decision_digest IS NOT NULL
                AND closed_at IS NOT NULL
                AND closure_reason IS NOT NULL
                AND btrim(closure_reason) <> ''
            ) OR (
                phase = 'denied'
                AND browser_nonce_digest IS NULL
                AND browser_identity_id IS NOT NULL
                AND browser_binding_digest IS NULL
                AND decision_digest IS NOT NULL
                AND reconnect_agent_id IS NULL
                AND closed_at IS NOT NULL
                AND closure_reason IS NOT NULL
                AND btrim(closure_reason) <> ''
            ) OR (
                phase IN ('expired', 'failed')
                AND browser_nonce_digest IS NULL
                AND browser_binding_digest IS NULL
                AND closed_at IS NOT NULL
                AND closure_reason IS NOT NULL
                AND btrim(closure_reason) <> ''
            )
            """,
            name="ck_enrollment_interactions_phase_shape",
        ),
        sa.ForeignKeyConstraint(
            ["client_software_id", "client_id"],
            ["client_software.client_software_id", "client_software.oauth_client_id"],
            name="fk_enrollment_interactions_exact_client_software",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "interaction_id",
            "client_id",
            "redirect_uri",
            "code_challenge",
            "correlation_release_after",
            name="uq_enrollment_interactions_correlation_component",
        ),
    )
    op.create_index("idx_enrollment_interactions_phase_expires_at", "enrollment_interactions", ["phase", "expires_at"])
    op.create_index("idx_enrollment_interactions_client_software_id", "enrollment_interactions", ["client_software_id"])

    op.create_table(
        "enrollment_correlation_reservations",
        sa.Column("interaction_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("release_after", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["interaction_id", "client_id", "redirect_uri", "code_challenge", "release_after"],
            [
                "enrollment_interactions.interaction_id",
                "enrollment_interactions.client_id",
                "enrollment_interactions.redirect_uri",
                "enrollment_interactions.code_challenge",
                "enrollment_interactions.correlation_release_after",
            ],
            name="fk_enrollment_correlation_reservations_exact_interaction",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "client_id", "redirect_uri", "code_challenge", name="uq_enrollment_correlation_reservations_tuple"
        ),
    )

    # Agent and AgentNameReservation deliberately form a deferred ownership cycle: an Agent can
    # never exist with a nullable current name, while a current reservation must be owned by that
    # exact Agent. The composite side is added after both tables exist.
    op.create_table(
        "agents",
        sa.Column("agent_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_operator_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operators.operator_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("current_name_reservation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", _agent_status(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'draft' AND activated_at IS NULL) "
            "OR (status = 'abandoned' AND activated_at IS NULL) "
            "OR (status IN ('active', 'disabled', 'deleted') AND activated_at IS NOT NULL)",
            name="ck_agents_status_shape",
        ),
    )
    op.create_index("idx_agents_owner_operator_id", "agents", ["owner_operator_id"])

    op.create_table(
        "agent_name_reservations",
        sa.Column("reservation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("display_name_key", sa.Text(), nullable=False),
        sa.Column(
            "originating_interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("enrollment_interactions.interaction_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "pending_interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("enrollment_interactions.interaction_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "num_nonnulls(pending_interaction_id, agent_id) = 1", name="ck_agent_name_reservations_exactly_one_owner"
        ),
        sa.CheckConstraint(
            "display_name ~ '[^[:space:][:cntrl:]\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'",
            name="ck_agent_name_reservations_display_name_nonempty",
        ),
        sa.CheckConstraint(
            "display_name_key ~ '[^[:space:][:cntrl:]\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'",
            name="ck_agent_name_reservations_key_nonempty",
        ),
        sa.CheckConstraint(
            "pending_interaction_id IS NULL OR originating_interaction_id = pending_interaction_id",
            name="ck_agent_name_reservations_pending_origin",
        ),
        sa.CheckConstraint("char_length(display_name) <= 80", name="ck_agent_name_reservations_display_name_length"),
        sa.CheckConstraint(
            "(agent_id IS NULL) = (activated_at IS NULL)", name="ck_agent_name_reservations_activation_shape"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="fk_agent_name_reservations_agent",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("display_name_key", name="uq_agent_name_reservations_display_name_key"),
        sa.UniqueConstraint("pending_interaction_id", name="uq_agent_name_reservations_pending_interaction"),
        sa.UniqueConstraint("agent_id", "reservation_id", name="uq_agent_name_reservations_agent_reservation"),
    )
    op.create_index("idx_agent_name_reservations_agent_id", "agent_name_reservations", ["agent_id"])
    op.create_foreign_key(
        "fk_agents_owned_current_name",
        "agents",
        "agent_name_reservations",
        ["agent_id", "current_name_reservation_id"],
        ["agent_id", "reservation_id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "credential_bindings",
        sa.Column("binding_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("kind", _credential_kind(), nullable=False),
        sa.Column("status", _credential_binding_status(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("supersedes_binding_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("generation > 0", name="ck_credential_bindings_generation_positive"),
        sa.CheckConstraint(
            """
            (status = 'issuing' AND issued_at IS NULL AND activated_at IS NULL
                AND ended_at IS NULL AND end_reason IS NULL)
            OR (status = 'issued' AND issued_at IS NOT NULL AND activated_at IS NULL
                AND ended_at IS NULL AND end_reason IS NULL)
            OR (status = 'active' AND issued_at IS NOT NULL AND activated_at IS NOT NULL
                AND ended_at IS NULL AND end_reason IS NULL)
            OR (status IN ('revoked', 'expired', 'failed') AND ended_at IS NOT NULL
                AND end_reason IS NOT NULL AND btrim(end_reason) <> '')
            """,
            name="ck_credential_bindings_status_shape",
        ),
        sa.CheckConstraint(
            "(issued_at IS NULL OR issued_at >= created_at) "
            "AND (activated_at IS NULL OR (issued_at IS NOT NULL AND activated_at >= issued_at)) "
            "AND (ended_at IS NULL OR ended_at >= COALESCE(activated_at, issued_at, created_at))",
            name="ck_credential_bindings_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "supersedes_binding_id"],
            ["credential_bindings.agent_id", "credential_bindings.binding_id"],
            name="fk_credential_bindings_same_agent_predecessor",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("agent_id", "binding_id", name="uq_credential_bindings_agent_binding"),
        sa.UniqueConstraint("agent_id", "generation", name="uq_credential_bindings_agent_generation"),
    )
    op.create_index("idx_credential_bindings_agent_id", "credential_bindings", ["agent_id"])
    op.create_index(
        "uq_credential_bindings_one_active_per_agent",
        "credential_bindings",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_foreign_key(
        "fk_enrollment_interactions_reconnect_predecessor",
        "enrollment_interactions",
        "credential_bindings",
        ["reconnect_agent_id", "reconnect_predecessor_binding_id"],
        ["agent_id", "binding_id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "authorization_grants",
        sa.Column("grant_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("binding_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "authorizing_identity_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oidc_identities.identity_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "client_software_id",
            UUID(as_uuid=True),
            sa.ForeignKey("client_software.client_software_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "enrollment_interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("enrollment_interactions.interaction_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("allowed_scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column("initial_access_jti", sa.Text(), nullable=True),
        sa.Column("initial_refresh_jti", sa.Text(), nullable=True),
        sa.Column("token_family_persisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "array_position(allowed_scopes, NULL) IS NULL", name="ck_authorization_grants_allowed_scopes_no_null"
        ),
        sa.CheckConstraint(
            "(token_family_persisted_at IS NULL AND initial_access_jti IS NULL "
            "AND initial_refresh_jti IS NULL) OR "
            "(token_family_persisted_at IS NOT NULL AND initial_access_jti IS NOT NULL "
            "AND btrim(initial_access_jti) <> '' "
            "AND (initial_refresh_jti IS NULL OR btrim(initial_refresh_jti) <> ''))",
            name="ck_authorization_grants_token_family_evidence_shape",
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["credential_bindings.binding_id"],
            name="fk_authorization_grants_binding",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("binding_id", name="uq_authorization_grants_binding_id"),
        sa.UniqueConstraint("enrollment_interaction_id", name="uq_authorization_grants_enrollment_interaction_id"),
    )
    op.create_index(
        "idx_authorization_grants_authorizing_identity_id", "authorization_grants", ["authorizing_identity_id"]
    )
    op.create_index("idx_authorization_grants_client_software_id", "authorization_grants", ["client_software_id"])

    op.create_table(
        "static_credentials",
        sa.Column("binding_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("secret_reference", sa.Text(), nullable=False),
        sa.Column("credential_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(secret_reference) <> ''", name="ck_static_credentials_secret_reference_nonempty"),
        sa.CheckConstraint(
            "octet_length(credential_fingerprint) > 0", name="ck_static_credentials_fingerprint_nonempty"
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["credential_bindings.binding_id"],
            name="fk_static_credentials_binding",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("credential_fingerprint", name="uq_static_credentials_fingerprint"),
    )


def _create_tool_call_principal_table() -> None:
    op.create_table(
        "mcp_tool_call_principals",
        sa.Column(
            "tool_call_id",
            sa.Text(),
            sa.ForeignKey("mcp_tool_calls.tool_call_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "operator_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operators.operator_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "binding_id",
            UUID(as_uuid=True),
            sa.ForeignKey("credential_bindings.binding_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "num_nonnulls(operator_id, binding_id) = 1", name="ck_mcp_tool_call_principals_exactly_one_variant"
        ),
    )
    op.create_index("idx_mcp_tool_call_principals_operator_id", "mcp_tool_call_principals", ["operator_id"])
    op.create_index("idx_mcp_tool_call_principals_binding_id", "mcp_tool_call_principals", ["binding_id"])


def _create_row_invariant_triggers() -> None:
    # Routine UPDATEs may refresh observation timestamps, but cannot rewrite the Operator or
    # provenance of an identity.
    op.execute(
        """
        CREATE FUNCTION haku_0009_identity_anchor_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_identity_anchor_immutable
        BEFORE UPDATE ON identity_anchors
        FOR EACH ROW EXECUTE FUNCTION haku_0009_identity_anchor_immutable()
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_oidc_identity_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.identity_id, NEW.anchor_id, NEW.issuer, NEW.subject, NEW.first_seen_at)
               IS DISTINCT FROM
               ROW(OLD.identity_id, OLD.anchor_id, OLD.issuer, OLD.subject, OLD.first_seen_at) THEN
                RAISE EXCEPTION 'OidcIdentity provenance and IdentityAnchor link are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_oidc_identity_immutable
        BEFORE UPDATE ON oidc_identities
        FOR EACH ROW EXECUTE FUNCTION haku_0009_oidc_identity_immutable()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_client_software_invariants() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_client_software_invariants
        BEFORE INSERT OR UPDATE ON client_software
        FOR EACH ROW EXECUTE FUNCTION haku_0009_client_software_invariants()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_enrollment_interaction_invariants() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_enrollment_interaction_invariants
        BEFORE INSERT OR UPDATE ON enrollment_interactions
        FOR EACH ROW EXECUTE FUNCTION haku_0009_enrollment_interaction_invariants()
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_enrollment_interaction_delete_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF clock_timestamp() < OLD.correlation_release_after THEN
                RAISE EXCEPTION 'EnrollmentInteraction cannot be deleted before correlation release'
                    USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_enrollment_interaction_delete_guard
        BEFORE DELETE ON enrollment_interactions
        FOR EACH ROW EXECUTE FUNCTION haku_0009_enrollment_interaction_delete_guard()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_correlation_reservation_invariants() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_correlation_reservation_invariants
        BEFORE UPDATE OR DELETE ON enrollment_correlation_reservations
        FOR EACH ROW EXECUTE FUNCTION haku_0009_correlation_reservation_invariants()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_agent_invariants() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_agent_invariants
        BEFORE INSERT OR UPDATE OR DELETE ON agents
        FOR EACH ROW EXECUTE FUNCTION haku_0009_agent_invariants()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_agent_name_invariants() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_agent_name_invariants
        BEFORE INSERT OR UPDATE OR DELETE ON agent_name_reservations
        FOR EACH ROW EXECUTE FUNCTION haku_0009_agent_name_invariants()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_credential_binding_invariants() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_credential_binding_invariants
        BEFORE INSERT OR UPDATE ON credential_bindings
        FOR EACH ROW EXECUTE FUNCTION haku_0009_credential_binding_invariants()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_authorization_grant_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_authorization_grant_immutable
        BEFORE UPDATE ON authorization_grants
        FOR EACH ROW EXECUTE FUNCTION haku_0009_authorization_grant_immutable()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_static_credential_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'StaticCredential rows are immutable; rotate the binding instead'
                USING ERRCODE = '23514';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_static_credential_immutable
        BEFORE UPDATE ON static_credentials
        FOR EACH ROW EXECUTE FUNCTION haku_0009_static_credential_immutable()
        """
    )

    op.execute(
        """
        CREATE FUNCTION haku_0009_tool_call_principal_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.tool_call_id, NEW.operator_id, NEW.binding_id)
               IS DISTINCT FROM ROW(OLD.tool_call_id, OLD.operator_id, OLD.binding_id) THEN
                RAISE EXCEPTION 'ToolCallPrincipal provenance is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_tool_call_principal_immutable
        BEFORE UPDATE ON mcp_tool_call_principals
        FOR EACH ROW EXECUTE FUNCTION haku_0009_tool_call_principal_immutable()
        """
    )


def _create_deferred_graph_invariants() -> None:
    # A new interaction must reserve its exact public tuple in the same transaction. After the
    # transaction-plus-code tombstone expires, cleanup deletes only the reservation; the immutable
    # interaction remains as grant/audit provenance.
    op.execute(
        """
        CREATE FUNCTION haku_0009_assert_correlation_reservation(target_interaction_id UUID)
        RETURNS void LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_new_interaction_correlation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM haku_0009_assert_correlation_reservation(NEW.interaction_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_new_interaction_has_correlation
        AFTER INSERT ON enrollment_interactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_new_interaction_correlation()
        """
    )

    # Phase is a discriminant over relational children: pending names exist only for a create-new
    # allow decision, and a resulting grant exists only once exchange has begun. Reconnect is the
    # other Allowed variant and must already target the browser Operator's active Agent/binding.
    op.execute(
        """
        CREATE FUNCTION haku_0009_assert_interaction_aggregate(target_interaction_id UUID)
        RETURNS void LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_aggregate_from_interaction() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_interaction_aggregate(NEW.interaction_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_aggregate_from_name() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_aggregate_from_grant() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_interaction_aggregate(OLD.enrollment_interaction_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_interaction_aggregate(NEW.enrollment_interaction_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_interaction_aggregate
        AFTER INSERT OR UPDATE ON enrollment_interactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_aggregate_from_interaction()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_name_interaction_aggregate
        AFTER INSERT OR UPDATE OR DELETE ON agent_name_reservations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_aggregate_from_name()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_grant_interaction_aggregate
        AFTER INSERT OR UPDATE OR DELETE ON authorization_grants
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_aggregate_from_grant()
        """
    )

    # A create-new name is not merely an Agent-owned string. Its immutable origin records the
    # exact approval interaction, and promotion is valid only if that interaction's OAuth grant
    # targets the same Agent. Static names and later non-enrollment renames have no origin.
    op.execute(
        """
        CREATE FUNCTION haku_0009_assert_name_promotion(target_reservation_id UUID)
        RETURNS void LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_name_promotion() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'DELETE' THEN
                PERFORM haku_0009_assert_name_promotion(NEW.reservation_id);
                RETURN NEW;
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_name_promotion
        AFTER INSERT OR UPDATE ON agent_name_reservations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_name_promotion()
        """
    )

    # CredentialBinding is the sole lifecycle row. Its enum kind is a relational discriminant:
    # OAuth has exactly one AuthorizationGrant, static has exactly one StaticCredential.
    op.execute(
        """
        CREATE FUNCTION haku_0009_assert_binding_subtype(target_binding_id UUID)
        RETURNS void LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_subtype_from_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_binding_subtype(OLD.binding_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_subtype(NEW.binding_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_subtype_from_grant() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_binding_subtype(OLD.binding_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_subtype(NEW.binding_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_subtype_from_static() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_binding_subtype(OLD.binding_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_subtype(NEW.binding_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_binding_subtype
        AFTER INSERT OR UPDATE OR DELETE ON credential_bindings
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_subtype_from_binding()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_grant_subtype
        AFTER INSERT OR UPDATE OR DELETE ON authorization_grants
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_subtype_from_grant()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_static_subtype
        AFTER INSERT OR UPDATE OR DELETE ON static_credentials
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_subtype_from_static()
        """
    )

    # Grant provenance joins rather than copies ownership. Both verified OIDC identities may come
    # from different configured issuers, but both must resolve through immutable anchors to the
    # same Operator as the Agent. Scopes may narrow, never broaden beyond the public request.
    op.execute(
        """
        CREATE FUNCTION haku_0009_lock_grant_authority() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0009_lock_grant_authority
        BEFORE INSERT OR UPDATE ON authorization_grants
        FOR EACH ROW EXECUTE FUNCTION haku_0009_lock_grant_authority()
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_assert_grant_consistency(
            target_grant_id UUID,
            require_initial_state BOOLEAN
        )
        RETURNS void LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_grant_consistency() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_grant_consistency(NEW.grant_id, TG_OP = 'INSERT');
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_grant_consistency
        AFTER INSERT OR UPDATE ON authorization_grants
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_grant_consistency()
        """
    )

    # Active replacement is a compare-and-set outcome: it must be the newest recorded generation,
    # its predecessor is terminal in the same committed graph, and the Agent itself is active.
    op.execute(
        """
        CREATE FUNCTION haku_0009_assert_binding_activation(target_binding_id UUID)
        RETURNS void LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_binding_activation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_binding_activation(NEW.binding_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_agent_active_bindings() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_binding_activation
        AFTER INSERT OR UPDATE ON credential_bindings
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_binding_activation()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_agent_active_bindings
        AFTER INSERT OR UPDATE ON agents
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_agent_active_bindings()
        """
    )

    # The principal row is a one-to-one relational union, not an optional sidecar. Deferral lets a
    # repository insert/delete the call and principal atomically in either statement order.
    op.execute(
        """
        CREATE FUNCTION haku_0009_assert_tool_call_principal(target_tool_call_id TEXT)
        RETURNS void LANGUAGE plpgsql AS $$
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
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_principal_from_call() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_tool_call_principal(NEW.tool_call_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION haku_0009_check_principal_from_principal() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM haku_0009_assert_tool_call_principal(OLD.tool_call_id);
                RETURN OLD;
            END IF;
            PERFORM haku_0009_assert_tool_call_principal(NEW.tool_call_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_call_has_principal
        AFTER INSERT OR UPDATE ON mcp_tool_calls
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_principal_from_call()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ctrg_haku_0009_principal_has_call
        AFTER INSERT OR UPDATE OR DELETE ON mcp_tool_call_principals
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION haku_0009_check_principal_from_principal()
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _tool_call_status_enum(create_type=True).create(bind, checkfirst=True)
    _operator_status_enum(create_type=True).create(bind, checkfirst=True)
    _client_registration_kind(create_type=True).create(bind, checkfirst=True)
    _enrollment_phase(create_type=True).create(bind, checkfirst=True)
    _agent_status(create_type=True).create(bind, checkfirst=True)
    _credential_kind(create_type=True).create(bind, checkfirst=True)
    _credential_binding_status(create_type=True).create(bind, checkfirst=True)

    _create_core_tables()
    _create_authority_tables()
    _create_tool_call_principal_table()
    _create_row_invariant_triggers()
    _create_deferred_graph_invariants()


def downgrade() -> None:
    raise RuntimeError("0010 is the forward-only Haku console baseline")
