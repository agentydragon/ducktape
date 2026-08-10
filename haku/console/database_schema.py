"""SQLAlchemy ORM schema for haku-console's database."""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from haku.console.agents.models import (
    MAX_AGENT_DISPLAY_NAME_LENGTH,
    AgentStatus,
    ClientRegistrationKind,
    CredentialBindingStatus,
    CredentialKind,
    EnrollmentPhase,
)
from haku.console.node_daemon_models import NodeDaemonExecutionStatus
from haku.console.operator_identity import OperatorStatus
from haku.console.provider_connection_registry import ProviderConnectionKind
from haku.console.tool_calls import ToolCallStatus
from util.sqlalchemy_types import StrEnumColumn, StringBackedStrEnumColumn


class Base(DeclarativeBase):
    pass


class Operator(Base):
    __tablename__ = "operators"

    operator_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    status: Mapped[OperatorStatus] = mapped_column(
        StrEnumColumn(OperatorStatus, name="operator_status"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityAnchor(Base):
    __tablename__ = "identity_anchors"
    __table_args__ = (
        UniqueConstraint(
            "trust_domain", "stable_external_user_key", name="uq_identity_anchors_trust_domain_external_key"
        ),
        CheckConstraint("btrim(trust_domain) <> ''", name="ck_identity_anchors_trust_domain_nonempty"),
        CheckConstraint("btrim(stable_external_user_key) <> ''", name="ck_identity_anchors_external_key_nonempty"),
        Index("idx_identity_anchors_operator_id", "operator_id"),
    )

    anchor_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="RESTRICT"), nullable=False
    )
    trust_domain: Mapped[str] = mapped_column(Text, nullable=False)
    stable_external_user_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OidcIdentity(Base):
    __tablename__ = "oidc_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_oidc_identities_issuer_subject"),
        CheckConstraint("btrim(issuer) <> ''", name="ck_oidc_identities_issuer_nonempty"),
        CheckConstraint("btrim(subject) <> ''", name="ck_oidc_identities_subject_nonempty"),
        Index("idx_oidc_identities_anchor_id", "anchor_id"),
    )

    identity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    anchor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity_anchors.anchor_id", ondelete="RESTRICT"), nullable=False
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClientSoftware(Base):
    __tablename__ = "client_software"
    __table_args__ = (
        UniqueConstraint("oauth_client_id", name="uq_client_software_oauth_client_id"),
        UniqueConstraint("client_software_id", "oauth_client_id", name="uq_client_software_id_oauth_client_id"),
        CheckConstraint("btrim(oauth_client_id) <> ''", name="ck_client_software_oauth_client_id_nonempty"),
        CheckConstraint(
            "cardinality(validated_redirect_uris) > 0", name="ck_client_software_validated_redirect_uris_nonempty"
        ),
        CheckConstraint(
            "array_position(validated_redirect_uris, NULL) IS NULL",
            name="ck_client_software_validated_redirect_uris_no_null",
        ),
        CheckConstraint("octet_length(metadata_hash) > 0", name="ck_client_software_metadata_hash_nonempty"),
    )

    client_software_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    registration_kind: Mapped[ClientRegistrationKind] = mapped_column(
        StrEnumColumn(ClientRegistrationKind, name="client_registration_kind"), nullable=False
    )
    oauth_client_id: Mapped[str] = mapped_column(Text, nullable=False)
    validated_redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    metadata_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    observed_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_icon_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EnrollmentInteraction(Base):
    __tablename__ = "enrollment_interactions"
    __table_args__ = (
        UniqueConstraint(
            "interaction_id",
            "client_id",
            "redirect_uri",
            "code_challenge",
            "correlation_release_after",
            name="uq_enrollment_interactions_correlation_component",
        ),
        CheckConstraint("btrim(client_id) <> ''", name="ck_enrollment_interactions_client_id_nonempty"),
        CheckConstraint("btrim(redirect_uri) <> ''", name="ck_enrollment_interactions_redirect_uri_nonempty"),
        CheckConstraint("btrim(code_challenge) <> ''", name="ck_enrollment_interactions_code_challenge_nonempty"),
        CheckConstraint(
            "btrim(upstream_authorization_url) <> ''", name="ck_enrollment_interactions_upstream_url_nonempty"
        ),
        CheckConstraint(
            "array_position(requested_scopes, NULL) IS NULL", name="ck_enrollment_interactions_requested_scopes_no_null"
        ),
        CheckConstraint(
            "correlation_release_after > expires_at", name="ck_enrollment_interactions_correlation_outlives_interaction"
        ),
        CheckConstraint(
            "browser_binding_digest IS NULL OR browser_identity_id IS NOT NULL",
            name="ck_enrollment_interactions_browser_binding_shape",
        ),
        CheckConstraint(
            "(reconnect_agent_id IS NULL) = (reconnect_predecessor_binding_id IS NULL)",
            name="ck_enrollment_interactions_reconnect_shape",
        ),
        CheckConstraint(
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
        ForeignKeyConstraint(
            ["client_software_id", "client_id"],
            ["client_software.client_software_id", "client_software.oauth_client_id"],
            name="fk_enrollment_interactions_exact_client_software",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reconnect_agent_id", "reconnect_predecessor_binding_id"],
            ["credential_bindings.agent_id", "credential_bindings.binding_id"],
            name="fk_enrollment_interactions_reconnect_predecessor",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("idx_enrollment_interactions_phase_expires_at", "phase", "expires_at"),
        Index("idx_enrollment_interactions_client_software_id", "client_software_id"),
    )

    interaction_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    client_software_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    requested_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    presentation_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    upstream_authorization_url: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[EnrollmentPhase] = mapped_column(
        StrEnumColumn(EnrollmentPhase, name="enrollment_phase"), nullable=False
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_release_after: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    browser_nonce_digest: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    browser_identity_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("oidc_identities.identity_id", ondelete="RESTRICT"), nullable=True
    )
    browser_binding_digest: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    decision_digest: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    reconnect_agent_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    reconnect_predecessor_binding_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    auto_approval_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    closure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EnrollmentCorrelationReservation(Base):
    __tablename__ = "enrollment_correlation_reservations"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "redirect_uri", "code_challenge", name="uq_enrollment_correlation_reservations_tuple"
        ),
        ForeignKeyConstraint(
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
    )

    interaction_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    release_after: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id", "current_name_reservation_id"],
            ["agent_name_reservations.agent_id", "agent_name_reservations.reservation_id"],
            name="fk_agents_owned_current_name",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(status = 'draft' AND activated_at IS NULL) OR "
            "(status = 'abandoned' AND activated_at IS NULL) OR "
            "(status IN ('active', 'disabled', 'deleted') AND activated_at IS NOT NULL)",
            name="ck_agents_status_shape",
        ),
        Index("idx_agents_owner_operator_id", "owner_operator_id"),
    )

    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="RESTRICT"), nullable=False
    )
    current_name_reservation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[AgentStatus] = mapped_column(StrEnumColumn(AgentStatus, name="agent_status"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Null is intentionally fail-closed and lets the migration distinguish existing Agents whose
    # deploy-time static assignment still needs to be seeded at the next reconciliation.
    auto_approval_policy: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentNameReservation(Base):
    __tablename__ = "agent_name_reservations"
    __table_args__ = (
        UniqueConstraint("display_name_key", name="uq_agent_name_reservations_display_name_key"),
        UniqueConstraint("pending_interaction_id", name="uq_agent_name_reservations_pending_interaction"),
        UniqueConstraint("agent_id", "reservation_id", name="uq_agent_name_reservations_agent_reservation"),
        CheckConstraint(
            "num_nonnulls(pending_interaction_id, agent_id) = 1", name="ck_agent_name_reservations_exactly_one_owner"
        ),
        CheckConstraint(
            "display_name ~ '[^[:space:][:cntrl:]\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'",
            name="ck_agent_name_reservations_display_name_nonempty",
        ),
        CheckConstraint(
            "display_name_key ~ '[^[:space:][:cntrl:]\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'",
            name="ck_agent_name_reservations_key_nonempty",
        ),
        CheckConstraint(
            "pending_interaction_id IS NULL OR originating_interaction_id = pending_interaction_id",
            name="ck_agent_name_reservations_pending_origin",
        ),
        CheckConstraint(
            f"char_length(display_name) <= {MAX_AGENT_DISPLAY_NAME_LENGTH}",
            name="ck_agent_name_reservations_display_name_length",
        ),
        CheckConstraint(
            "(agent_id IS NULL) = (activated_at IS NULL)", name="ck_agent_name_reservations_activation_shape"
        ),
        Index("idx_agent_name_reservations_agent_id", "agent_id"),
    )

    reservation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name_key: Mapped[str] = mapped_column(Text, nullable=False)
    originating_interaction_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("enrollment_interactions.interaction_id", ondelete="RESTRICT"), nullable=True
    )
    pending_interaction_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("enrollment_interactions.interaction_id", ondelete="RESTRICT"), nullable=True
    )
    agent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agents.agent_id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CredentialBinding(Base):
    __tablename__ = "credential_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", "binding_id", name="uq_credential_bindings_agent_binding"),
        UniqueConstraint("agent_id", "generation", name="uq_credential_bindings_agent_generation"),
        ForeignKeyConstraint(
            ["agent_id", "supersedes_binding_id"],
            ["credential_bindings.agent_id", "credential_bindings.binding_id"],
            name="fk_credential_bindings_same_agent_predecessor",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("generation > 0", name="ck_credential_bindings_generation_positive"),
        CheckConstraint(
            """
            (status = 'issuing' AND issued_at IS NULL AND activated_at IS NULL AND ended_at IS NULL AND end_reason IS NULL)
            OR (status = 'issued' AND issued_at IS NOT NULL AND activated_at IS NULL AND ended_at IS NULL AND end_reason IS NULL)
            OR (status = 'active' AND issued_at IS NOT NULL AND activated_at IS NOT NULL AND ended_at IS NULL AND end_reason IS NULL)
            OR (status IN ('revoked', 'expired', 'failed') AND ended_at IS NOT NULL
                AND end_reason IS NOT NULL AND btrim(end_reason) <> '')
            """,
            name="ck_credential_bindings_status_shape",
        ),
        CheckConstraint(
            "(issued_at IS NULL OR issued_at >= created_at) "
            "AND (activated_at IS NULL OR (issued_at IS NOT NULL AND activated_at >= issued_at)) "
            "AND (ended_at IS NULL OR ended_at >= COALESCE(activated_at, issued_at, created_at))",
            name="ck_credential_bindings_timestamp_order",
        ),
        Index("idx_credential_bindings_agent_id", "agent_id"),
        Index(
            "uq_credential_bindings_one_active_per_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    binding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[CredentialKind] = mapped_column(StrEnumColumn(CredentialKind, name="credential_kind"), nullable=False)
    status: Mapped[CredentialBindingStatus] = mapped_column(
        StrEnumColumn(CredentialBindingStatus, name="credential_binding_status"), nullable=False
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    supersedes_binding_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuthorizationGrant(Base):
    __tablename__ = "authorization_grants"
    __table_args__ = (
        UniqueConstraint("binding_id", name="uq_authorization_grants_binding_id"),
        UniqueConstraint("enrollment_interaction_id", name="uq_authorization_grants_enrollment_interaction_id"),
        CheckConstraint(
            "array_position(allowed_scopes, NULL) IS NULL", name="ck_authorization_grants_allowed_scopes_no_null"
        ),
        CheckConstraint(
            "(token_family_persisted_at IS NULL AND initial_access_jti IS NULL AND initial_refresh_jti IS NULL) "
            "OR (token_family_persisted_at IS NOT NULL AND initial_access_jti IS NOT NULL "
            "AND btrim(initial_access_jti) <> '' "
            "AND (initial_refresh_jti IS NULL OR btrim(initial_refresh_jti) <> ''))",
            name="ck_authorization_grants_token_family_evidence_shape",
        ),
        Index("idx_authorization_grants_authorizing_identity_id", "authorizing_identity_id"),
        Index("idx_authorization_grants_client_software_id", "client_software_id"),
    )

    grant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    binding_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("credential_bindings.binding_id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        nullable=False,
    )
    authorizing_identity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("oidc_identities.identity_id", ondelete="RESTRICT"), nullable=False
    )
    client_software_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("client_software.client_software_id", ondelete="RESTRICT"), nullable=False
    )
    enrollment_interaction_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("enrollment_interactions.interaction_id", ondelete="RESTRICT"), nullable=False
    )
    allowed_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    initial_access_jti: Mapped[str | None] = mapped_column(Text, nullable=True)
    initial_refresh_jti: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_family_persisted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StaticCredential(Base):
    __tablename__ = "static_credentials"
    __table_args__ = (
        UniqueConstraint("credential_fingerprint", name="uq_static_credentials_fingerprint"),
        CheckConstraint("btrim(secret_reference) <> ''", name="ck_static_credentials_secret_reference_nonempty"),
        CheckConstraint("octet_length(credential_fingerprint) > 0", name="ck_static_credentials_fingerprint_nonempty"),
    )

    binding_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("credential_bindings.binding_id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        primary_key=True,
    )
    secret_reference: Mapped[str] = mapped_column(Text, nullable=False)
    credential_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class McpToolCall(Base):
    __tablename__ = "mcp_tool_calls"
    __table_args__ = (Index("idx_mcp_tool_calls_created_at", "created_at"),)

    tool_call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        StrEnumColumn(ToolCallStatus, name="tool_call_status"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    withdrawal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_policy_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_approval_evaluation: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpToolCallPrincipal(Base):
    __tablename__ = "mcp_tool_call_principals"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(operator_id, binding_id) = 1", name="ck_mcp_tool_call_principals_exactly_one_variant"
        ),
        Index("idx_mcp_tool_call_principals_operator_id", "operator_id"),
        Index("idx_mcp_tool_call_principals_binding_id", "binding_id"),
    )

    tool_call_id: Mapped[str] = mapped_column(
        Text, ForeignKey("mcp_tool_calls.tool_call_id", ondelete="CASCADE"), primary_key=True
    )
    operator_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="RESTRICT"), nullable=True
    )
    binding_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("credential_bindings.binding_id", ondelete="RESTRICT"), nullable=True
    )


class NodeDaemonPresence(Base):
    __tablename__ = "node_daemon_presence"

    daemon_id: Mapped[str] = mapped_column(Text, primary_key=True)
    instance_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    backends_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    capacity: Mapped[int] = mapped_column(nullable=False)
    connected_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NodeDaemonExecution(Base):
    __tablename__ = "node_daemon_executions"
    __table_args__ = (Index("idx_node_daemon_executions_dispatch", "daemon_id", "status", "created_at"),)

    execution_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    daemon_id: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NodeDaemonExecutionStatus] = mapped_column(
        StrEnumColumn(NodeDaemonExecutionStatus, name="node_daemon_execution_status"), nullable=False
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatch_expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    instance_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    lease_token_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthTokenState(Base):
    """Current access/refresh token state shared by every Operator OAuth association."""

    __tablename__ = "oauth_token_states"
    __table_args__ = (
        UniqueConstraint("token_state_id", "operator_id", name="uq_oauth_token_states_id_operator"),
        CheckConstraint(
            "(refresh_claim_id IS NULL) = (refresh_claim_expires_at IS NULL)",
            name="ck_oauth_token_states_refresh_claim_shape",
        ),
        CheckConstraint(
            """
            (refresh_failure_count = 0
                AND refresh_failure_started_at IS NULL
                AND refresh_failure_initial_kind IS NULL
                AND refresh_failure_initial_message IS NULL
                AND refresh_failure_latest_at IS NULL
                AND refresh_failure_latest_kind IS NULL
                AND refresh_failure_latest_message IS NULL
                AND refresh_failure_action IS NULL
                AND refresh_retry_at IS NULL)
            OR
            (refresh_failure_count > 0
                AND refresh_failure_started_at IS NOT NULL
                AND refresh_failure_initial_kind IS NOT NULL
                AND refresh_failure_initial_message IS NOT NULL
                AND refresh_failure_latest_at IS NOT NULL
                AND refresh_failure_latest_kind IS NOT NULL
                AND refresh_failure_latest_message IS NOT NULL
                AND ((refresh_failure_action = 'retrying' AND refresh_retry_at IS NOT NULL)
                    OR (refresh_failure_action IN ('reconnect', 'operator_action') AND refresh_retry_at IS NULL)))
            """,
            name="ck_oauth_token_states_refresh_failure_shape",
        ),
        Index(
            "idx_oauth_token_states_refresh_candidates",
            "token_expires_at",
            postgresql_where=text("refresh_token IS NOT NULL AND token_expires_at IS NOT NULL"),
        ),
    )

    token_state_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    token_revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_claim_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    refresh_claim_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_failure_started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_failure_initial_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_failure_initial_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_failure_latest_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_failure_latest_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_failure_latest_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_failure_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    refresh_failure_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_retry_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpOperatorOAuthAssociation(Base):
    __tablename__ = "mcp_operator_oauth_associations"

    server_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    association_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid4, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_state_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_state: Mapped[OAuthTokenState] = relationship(
        cascade="all, delete-orphan", single_parent=True, lazy="selectin"
    )
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("association_id", name="uq_mcp_operator_oauth_associations_association_id"),
        UniqueConstraint("token_state_id", name="uq_mcp_operator_oauth_associations_token_state_id"),
        ForeignKeyConstraint(
            ["token_state_id", "operator_id"],
            ["oauth_token_states.token_state_id", "oauth_token_states.operator_id"],
            name="fk_mcp_operator_oauth_associations_token_state",
            ondelete="CASCADE",
        ),
        Index("idx_mcp_operator_oauth_associations_operator", "operator_id"),
    )


class McpOperatorOAuthFlow(Base):
    __tablename__ = "mcp_operator_oauth_flows"
    __table_args__ = (
        Index("idx_mcp_operator_oauth_flows_server_operator", "server_id", "operator_id"),
        Index("idx_mcp_operator_oauth_flows_expires_at", "expires_at"),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProviderConnection(Base):
    """One Operator's linked account for a well-known OAuth provider (Google today).

    One row per deploy-named ``(operator_id, connection_name)``. ``provider_name`` records the
    configured OAuth application that issued the grant, while ``provider`` records its protocol
    kind. The connection owns one shared ``OAuthTokenState`` refreshed with that same client.
    """

    __tablename__ = "provider_connections"
    operator_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    connection_name: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[ProviderConnectionKind] = mapped_column(
        StringBackedStrEnumColumn(ProviderConnectionKind), nullable=False
    )
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid4, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_state_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_state: Mapped[OAuthTokenState] = relationship(
        cascade="all, delete-orphan", single_parent=True, lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("connection_id", name="uq_provider_connections_connection_id"),
        UniqueConstraint("token_state_id", name="uq_provider_connections_token_state_id"),
        CheckConstraint("btrim(connection_name) <> ''", name="ck_provider_connections_connection_name_nonempty"),
        CheckConstraint("btrim(provider_name) <> ''", name="ck_provider_connections_provider_name_nonempty"),
        ForeignKeyConstraint(
            ["token_state_id", "operator_id"],
            ["oauth_token_states.token_state_id", "oauth_token_states.operator_id"],
            name="fk_provider_connections_token_state",
            ondelete="CASCADE",
        ),
        Index("idx_provider_connections_operator", "operator_id"),
    )


class ProviderConnectionFlow(Base):
    """Short-lived authorization-code + PKCE flow state for a pending provider connection."""

    __tablename__ = "provider_connection_flows"
    __table_args__ = (
        CheckConstraint("btrim(connection_name) <> ''", name="ck_provider_connection_flows_connection_name_nonempty"),
        CheckConstraint("btrim(provider_name) <> ''", name="ck_provider_connection_flows_provider_name_nonempty"),
        Index("idx_provider_connection_flows_operator", "operator_id"),
        Index("idx_provider_connection_flows_expires_at", "expires_at"),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    connection_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[ProviderConnectionKind] = mapped_column(
        StringBackedStrEnumColumn(ProviderConnectionKind), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)


class OAuthConnectionResult(Base):
    """A short-lived, single-use browser handoff after an account-link callback.

    The browser receives only the opaque ``result_id``. The outcome stays server-side, is
    bound to the Operator who started the flow, and is consumed by the trusted console SPA.
    """

    __tablename__ = "oauth_connection_results"
    __table_args__ = (
        CheckConstraint("status IN ('success', 'error')", name="ck_oauth_connection_results_status"),
        CheckConstraint("btrim(title) <> ''", name="ck_oauth_connection_results_title_nonempty"),
        CheckConstraint("btrim(message) <> ''", name="ck_oauth_connection_results_message_nonempty"),
        Index("idx_oauth_connection_results_operator", "operator_id"),
        Index("idx_oauth_connection_results_expires_at", "expires_at"),
    )

    result_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperatorLoginFlow(Base):
    """Short-lived authorization-code flow state for one pending operator browser login.

    Pre-authentication, so unlike every other flow table this one has no Operator: the row exists
    before any identity is known. It lives here rather than in the session cookie because authlib's
    session-backed state keeps exactly **one** pending login per browser — it clears every prior
    ``_state_<name>_*`` entry whenever it stores a new one — so a second console tab starting a
    login would strand the first one on "expired or was superseded". One row per attempt lets any
    number of tabs authenticate independently.

    ``browser_binding`` is the user-agent binding RFC 6749 §10.12 asks for: a secret handed to the
    browser in a cookie named after this flow's ``state``, so concurrent attempts cannot overwrite
    each other's. ``return_to`` rides the flow rather than the session for the same reason — it is
    *this* attempt's destination, not the browser's.
    """

    __tablename__ = "operator_login_flows"
    __table_args__ = (
        CheckConstraint("btrim(browser_binding) <> ''", name="ck_operator_login_flows_browser_binding_nonempty"),
        Index("idx_operator_login_flows_expires_at", "expires_at"),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    browser_binding: Mapped[str] = mapped_column(Text, nullable=False)
    return_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperatorAuthentikToken(Base):
    """The acting Operator's own Authentik OAuth token, captured at browser login (offline_access).

    One row per Operator. The console self-refreshes the associated token state using
    the operator-OIDC client (injected from Settings, never stored here); the ``hostexec`` server
    then exchanges that access token for a short-lived per-host token, so the operator acts under
    their own Authentik identity with no bespoke console key. The shared token state's revision
    guards a concurrent refresh/re-login.
    """

    __tablename__ = "operator_authentik_tokens"

    operator_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_state_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_state: Mapped[OAuthTokenState] = relationship(cascade="all, delete-orphan", single_parent=True)

    __table_args__ = (
        UniqueConstraint("token_state_id", name="uq_operator_authentik_tokens_token_state_id"),
        ForeignKeyConstraint(
            ["token_state_id", "operator_id"],
            ["oauth_token_states.token_state_id", "oauth_token_states.operator_id"],
            name="fk_operator_authentik_tokens_token_state",
            ondelete="CASCADE",
        ),
    )


class ClaudeChatSession(Base):
    """One Operator-owned Claude conversation and its Agent Sandbox rendezvous."""

    __tablename__ = "claude_chat_sessions"

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    bridge_token_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    bridge_connected_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('provisioning','ready','responding','closing','closed','failed')",
            name="ck_claude_chat_sessions_status",
        ),
        Index("idx_claude_chat_sessions_operator", "operator_id", "created_at"),
    )


class ClaudeChatMessage(Base):
    """A prompt or streamed assistant answer in one Claude chat session."""

    __tablename__ = "claude_chat_messages"

    message_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claude_chat_sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_uses: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="ck_claude_chat_messages_role"),
        CheckConstraint("status IN ('pending','streaming','complete','failed')", name="ck_claude_chat_messages_status"),
        Index("idx_claude_chat_messages_session_created", "session_id", "created_at"),
    )


class PushSubscription(Base):
    """One browser Push API subscription an Operator has granted this console.

    The ``endpoint`` a push service issues is both the address and the capability to push to that
    browser, so it is the natural identity: re-subscribing the same device (permission re-grant,
    key rotation, browser-initiated refresh) overwrites its row rather than accumulating dead
    ones. ``p256dh`` and ``auth`` are that subscription's own RFC 8291 encryption inputs — the
    console encrypts each payload to them, so the push service relays ciphertext it cannot read.

    Subscriptions die silently and often (permission revoked, browser profile cleared, endpoint
    expired). A push service reports that as 404/410, which prunes the row; ``last_failure_at``
    records the transient failures that do not.
    """

    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-text, operator-facing only: lets the Settings panel say *which* device a row is, so
    # revoking the right one does not require reading endpoint URLs.
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_failure_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_push_subscriptions_operator_id", "operator_id"),)


metadata = Base.metadata


class MatrixSyncState(Base):
    """Where the Matrix sync loop got to, and the token it got there with.

    One row per bot user. `next_batch` is the watermark that makes an outage replay
    rather than skip; `access_token` is cached because Synapse rate-limits `/login`.
    """

    __tablename__ = "matrix_sync_state"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_batch: Mapped[str | None] = mapped_column(Text, nullable=True)


class MatrixConversation(Base):
    """The one room Haku services, and the chat session bound to it.

    Keyed by bot user rather than by room, which is what makes "one room at a time"
    (R3.6a) a property of the schema instead of a rule the code has to remember: a second
    room cannot be recorded without displacing the first.

    Deliberately separate from `matrix_sync_state` even though both are singletons keyed
    the same way. The sync loop and the session supervisor run as independent tasks under
    one advisory lock, and giving them separate rows keeps a slow session claim from
    contending with the watermark write on every pass.
    """

    __tablename__ = "matrix_conversation"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The chat session currently serving the room. Null between a session dying and the
    # supervisor replacing it — an expected state, not a broken one.
    session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claude_chat_sessions.session_id", ondelete="SET NULL"), nullable=True
    )
    joined_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
