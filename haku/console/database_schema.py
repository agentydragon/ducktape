"""SQLAlchemy ORM schema for haku-console's database."""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
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
from haku.console.chat_models import (
    AuthoredEventKind,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    ConversationEventKind,
    EventProvenance,
    FrameDirection,
    SessionStatus,
    StoredEventKind,
    TurnOutcome,
)
from haku.console.node_daemon_models import NodeDaemonExecutionStatus
from haku.console.operator_identity import OperatorStatus
from haku.console.provider_connection_registry import ProviderConnectionKind
from haku.console.tool_calls import ToolCallStatus
from util.sqlalchemy_types import (
    StrEnumColumn,
    StringBackedStrEnumColumn,
    TextBackedStrEnumColumn,
    TextBackedStrEnumUnionColumn,
)


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
    # NULL is fail-closed: this Agent's deploy-time static assignment has not been seeded yet.
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

    Pre-authentication, so unlike every other flow table this one has no Operator. One row per
    attempt rather than session-cookie state, which authlib keeps exactly **one** of per browser —
    so a second console tab starting a login would strand the first on "expired or was superseded".

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

    One row per Operator. The console self-refreshes the associated token state using the
    operator-OIDC client (injected from Settings, never stored here); the ``hostexec`` server then
    exchanges that access token for a short-lived per-host token. The shared token state's revision
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


class Conversation(Base):
    """A thread that outlives the sessions running it — identity, and nothing else.

    Sessions come and go under it and channels attach to it. Replacing a dead session is creating a
    new one with the same `conversation_id`; its attachments are not touched, because they were
    never the session's.

    **A conversation never ends.** No `ended_at` and no terminal state: it is an id. "Start this room
    over" is detaching the address and attaching it to a new conversation, which
    `uq_chat_attachment_live_address` already permits — so a surface listing these needs keyset
    paging, since the list only grows.

    **A session attached to nothing stays expressible**: a conversation with one session and no
    attachment row, which is what an SPA session is.
    """

    __tablename__ = "conversation"

    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_conversation_operator", "operator_id", "created_at"),)


class ChatAttachment(Base):
    """One channel holding a copy of a conversation, at the address it holds it under.

    **Copy-holding channels only.** A row exists to hold a cursor, and a cursor exists because the
    channel keeps a copy the console owes work against — a Matrix room does, a browser tab does not.
    So `ck_chat_attachment_surface` admits no `spa` row, there is no synthetic address for a tab,
    and "the browser is looking at this conversation" is an absence rather than a row.

    Attach and detach are the row's whole lifecycle: `detached_at IS NULL` is the live binding, and
    past ones stay readable beside it.

    Which session serves an address needs no agreement between two rows: it is the live session of
    the conversation this attachment names.
    """

    __tablename__ = "chat_attachment"

    attachment_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False
    )
    surface: Mapped[ChatSurface] = mapped_column(TextBackedStrEnumColumn(ChatSurface), nullable=False)
    # What the channel calls this conversation: a Matrix room id today. Opaque here — only the
    # channel that holds the copy knows how to reach it.
    address: Mapped[str] = mapped_column(Text, nullable=False)
    attached_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detached_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("surface IN ('matrix')", name="ck_chat_attachment_surface"),
        CheckConstraint("btrim(address) <> ''", name="ck_chat_attachment_address_nonempty"),
        CheckConstraint(
            "detached_at IS NULL OR detached_at >= attached_at", name="ck_chat_attachment_detach_after_attach"
        ),
        Index(
            "uq_chat_attachment_live_address",
            "surface",
            "address",
            unique=True,
            postgresql_where=text("detached_at IS NULL"),
        ),
        Index("idx_chat_attachment_conversation", "conversation_id", "attached_at"),
    )


class ChatDelivery(Base):
    """What one channel put in its own copy of a conversation, and where it put it.

    Correspondence, stored rather than re-read: a channel holding a copy has to know whether it has
    already shown a thing before it shows it again. Reading the room back and parsing the tag off
    every event stays the repair path for a copy that drifted; this is the cheap one
    (<plans/conversation_layers.md> § 5).

    **Both columns are opaque outside the channel that wrote them**, as `chat_attachment.address`
    is: `sent_ref` is where the channel put it — a Matrix `event_id` today — and `subject` is what
    the channel decided to show there. Only the channel that minted a pair may look inside one;
    everything else compares, and never interprets. The pairing is per attachment, which is what
    makes two rooms holding one conversation two independent copies.

    **Live means the channel still shows it.** `retired_at` is set when the channel takes it back —
    a redacted status line — and `uq_chat_delivery_live_subject` is what makes re-deriving a subject
    find the event already showing it instead of sending a second one. A retired row stays: the
    channel's copy cannot be asked what it used to show.
    """

    __tablename__ = "chat_delivery"

    delivery_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chat_attachment.attachment_id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    sent_ref: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("btrim(subject) <> ''", name="ck_chat_delivery_subject_nonempty"),
        CheckConstraint("btrim(sent_ref) <> ''", name="ck_chat_delivery_ref_nonempty"),
        CheckConstraint("retired_at IS NULL OR retired_at >= sent_at", name="ck_chat_delivery_retire_after_sent"),
        Index(
            "uq_chat_delivery_live_subject",
            "attachment_id",
            "subject",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
    )


class Session(Base):
    """One Operator-owned agent conversation and its Agent Sandbox rendezvous."""

    __tablename__ = "sessions"

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    # The thread this session runs. Successive sessions serving one Matrix room share it, which is
    # what makes a replacement session inherit the room's attachments instead of being re-pointed at.
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False
    )
    surface: Mapped[ChatSurface] = mapped_column(TextBackedStrEnumColumn(ChatSurface), nullable=False)
    # The Matrix room this session serves — the *history* half of the binding, where
    # `matrix_conversation.session_id` is the live pointer. Written once at creation and never
    # updated, which is what keeps a replaced Matrix session distinguishable from an SPA one.
    room_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SessionStatus] = mapped_column(TextBackedStrEnumColumn(SessionStatus), nullable=False)
    # The verifier for this session's rendezvous credential — SHA-256 of a bearer minted once at
    # creation and never stored. It answers only "does this redialling runner hold this session's
    # token"; admissibility is the status's business, and the sandbox claim is `claim_cleaned_at`'s.
    bridge_token_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    bridge_connected_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this session's Agent Sandbox claim was deleted, and NULL until it has been — which is
    # what puts an ended session in `SessionStore.claim_cleanup_candidates` and what takes it back
    # out. Only ever stamped on a session whose status is already ended.
    claim_cleaned_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How far the fold has got: the `frame_seq` of the last frame whose projected effects are
    # committed. Written in the same transaction as those effects, which is what makes them
    # exactly-once — whoever adopts the session re-projects from here, redoing exactly the frames
    # whose effects did not commit.
    #
    # A bound like `session_turns.first_frame_seq`, not a pointer: `Identity` leaves gaps, and
    # `next_prompt` anchors it at the frame before the turn it opens, which is a value no row has.
    # **`0` is "nothing here has ever projected"**, which no frame's `frame_seq` can be, so the
    # bound needs no absent state; it arrives by the server default.
    projected_frame_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Renewed by whichever replica currently holds this session's runner websocket: a replica that
    # dies mid-turn otherwise leaves the row claiming a turn is in flight forever. Required, because
    # the sweep looks for a lease that has passed and an absent lease never does.
    lease_expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Which replica is asserting the lease, and — by being NULL or not — which of the lease's two
    # meanings is running. NULL is the creator's provisioning grant: nobody holds this session
    # yet, it is merely budgeted until a runner attaches. Set means that pod's heartbeat, so an
    # expired lease names the process to go read; `HOSTNAME` is the pod name `kubectl logs` takes.
    # Permanently nullable: "no holder yet" is a real state.
    lease_holder: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('idle','provisioning','ready','responding','closing','closed','failed')",
            name="ck_sessions_status",
        ),
        CheckConstraint("surface IN ('spa','matrix')", name="ck_sessions_surface"),
        # A session serving a room is what `matrix` means, so surface and room are one fact.
        CheckConstraint("(surface = 'matrix') = (room_id IS NOT NULL)", name="ck_sessions_matrix_room"),
        Index("idx_sessions_operator", "operator_id", "created_at"),
        Index("idx_sessions_conversation", "conversation_id", "created_at"),
        Index(
            "idx_sessions_expired_lease",
            "lease_expires_at",
            postgresql_where=text("status IN ('provisioning','ready','responding')"),
        ),
    )


class SessionMessage(Base):
    """A prompt or streamed assistant answer in one session."""

    __tablename__ = "session_messages"

    message_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ChatMessageRole] = mapped_column(TextBackedStrEnumColumn(ChatMessageRole), nullable=False)
    status: Mapped[ChatMessageStatus] = mapped_column(TextBackedStrEnumColumn(ChatMessageStatus), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # The agent's own id for the message this row records (`msg_…`): an `assistant` frame carries
    # the same id, so a reader can find exactly the tool calls that message made.
    #
    # NULL where there is nothing to point at: a user row, a row written before this column, or an
    # assistant row the console synthesized rather than observed. Nothing keys on it — a message
    # finds its calls through its frame range (`x/session_views.message_view`).
    agent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Inclusive range in the session's raw frame log this projection was built from — sequence
    # pointers rather than copied payloads, the frame log remaining the authoritative, lossless
    # record. Session-scoped; frame_seq is globally allocated and is not a foreign key by itself.
    #
    # **NULL is the operator's own prompt**, written before the frame it goes out as exists and
    # never pointed at all if no turn claims it. A live state rather than an era, so the range is
    # required by role in the constraint below rather than on these columns.
    # The range is what makes the rendering appealable: a projection nobody can read back to the
    # frames it came from is a projection nobody can debug.
    source_first_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_last_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="ck_session_messages_role"),
        CheckConstraint("status IN ('pending','streaming','complete','failed')", name="ck_session_messages_status"),
        CheckConstraint(
            "source_first_frame_seq IS NULL OR source_last_frame_seq IS NULL "
            "OR source_first_frame_seq <= source_last_frame_seq",
            name="ck_session_messages_source_frames",
        ),
        # Separate from the ordering rule above so a failure names its own cause: a far end with
        # no near end is neither a range nor the absence of one.
        CheckConstraint(
            "source_last_frame_seq IS NULL OR source_first_frame_seq IS NOT NULL",
            name="ck_session_messages_source_anchored",
        ),
        # Every writer of an assistant row names the frame that opened it. A user row is unpointed
        # exactly while its prompt is unclaimed, which is why this is by role and not on the column.
        CheckConstraint(
            "role <> 'assistant' OR source_first_frame_seq IS NOT NULL", name="ck_session_messages_assistant_pointed"
        ),
        Index("idx_session_messages_session_created", "session_id", "created_at"),
    )


class SessionPrompt(Base):
    """One prompt waiting to be handed to the model, or the record that it was.

    Holds no copy of the prompt's text: `message_id` is the transcript row minted with it, so the
    queue and the transcript cannot come to disagree about what was asked. "One prompt in flight"
    is the partial unique index below.
    """

    __tablename__ = "session_prompts"

    prompt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session_messages.message_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    queued_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the harness took it. Absent means still waiting — the state the index below makes at
    # most one of per session.
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_session_prompts_session", "session_id", "queued_at"),
        Index("uq_session_prompts_unclaimed", "session_id", unique=True, postgresql_where=text("claimed_at IS NULL")),
    )


class SessionTurn(Base):
    """One exchange, recorded as a range over the session's frame log.

    **A range, not a `turn_id` stamped on each frame.** The frame log is the record of the wire
    and the wire does not agree with our bracketing: the CLI folds a prompt sent mid-turn into
    the running one, so a single `result` can answer two prompts (measured,
    <../cli_protocol/probes/steering.py>). Keeping the bracket here means re-bracketing later is
    an update to this table rather than a rewrite of the record.

    **And the state of the exchange, not only its extent.** The last three columns are written in
    the same transaction as the effect each one describes, so a turn adopted by another replica is
    *read* rather than guessed at.
    """

    __tablename__ = "session_turns"

    turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    # Lower bound, taken when the turn opens: every frame of this turn has
    # `frame_seq >= first_frame_seq`. Not the seq of an actual frame — `Identity` leaves gaps and
    # the turn is opened before its first frame is written.
    first_frame_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Inclusive upper bound, taken when the turn closes. NULL on an open turn, and also on a
    # closed one that recorded nothing at all; `ended_at` is what says which.
    last_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[TurnOutcome | None] = mapped_column(TextBackedStrEnumColumn(TurnOutcome), nullable=True)
    # The assistant message this turn is streaming into, set when the message is opened and
    # cleared when it completes — so on an open turn it is non-NULL exactly while an answer is
    # half written. Whoever adopts the turn continues that message instead of forking a second one
    # beside it. On a closed turn it is left as it was, naming the message a failed turn stopped in.
    assistant_message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session_messages.message_id", ondelete="SET NULL"), nullable=True
    )
    # Whether an assistant message of this turn has completed. What the end of the turn asks
    # before minting a message for text that arrived only on the `result` frame: with one already
    # written, that text is a repeat of it rather than the turn's only words.
    said_anything: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Whether this turn has put a row in the room's outbox. Written in the same transaction as
    # that row, so it cannot claim a reply the outbox does not hold. It is **not** delivery —
    # `session_outbox.sent_at` is the only thing that says the room heard it — and not "a send was
    # attempted" either.
    queued_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','aborted','failed')", name="ck_session_turns_outcome"
        ),
        # Open and un-outcomed are the same state, so neither can be reached without the other.
        CheckConstraint("(ended_at IS NULL) = (outcome IS NULL)", name="ck_session_turns_ended_has_outcome"),
        Index("idx_session_turns_session", "session_id", "started_at"),
        # One open turn per session, as a schema property rather than a rule the turn loop has to
        # keep. Also what `responding` is derived from, and what makes "an abort names a turn" a
        # lookup rather than a guess.
        Index("uq_session_turns_open", "session_id", unique=True, postgresql_where=text("ended_at IS NULL")),
    )


class SessionTurnPrompt(Base):
    """Which prompts a turn answered.

    Many-to-one on purpose: a prompt written to the CLI while a turn is running is absorbed at
    the next tool boundary, and one `result` then covers both. Nothing sends a second prompt
    mid-turn yet.
    """

    __tablename__ = "session_turn_prompts"

    turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session_turns.turn_id", ondelete="CASCADE"), primary_key=True
    )
    message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session_messages.message_id", ondelete="CASCADE"), primary_key=True
    )


class SessionEvent(Base):
    """What the fold made of a session's frames, as rows: the neutral conversation, stored.

    The vocabulary is `x/conversation_events.py` — what the agent said, what it thought, the
    activity the harness narrated, and the tool calls it made **with their answers**, which no
    other row holds. Written inside `SessionStore.apply_frame`'s transaction, beside the message
    row and `sessions.projected_frame_seq`, so a row exists here exactly when the cursor says its
    frame was projected.

    **A row is found by frame range, not by the agent's id for a message.** `session_messages`
    records the inclusive span it was built from and an event's own span falls inside it, so the
    lookup needs nothing from the wire — `agent_message_id` is absent on thousands of production
    rows.

    **Deliberately not one row per `TextDelta`.** A delta is an increment of prose that the
    message's own row carries whole, at hundreds per turn; the `stream_event` frames it was cut
    from are in `session_frames`, which is where an operator appeals the joined text to the wire.

    **One ordered stream, two categories, split by where a row came from rather than by what it is
    about.** `ConversationEventKind` is what folding a recorded frame produced; `AuthoredEventKind`
    is what no frame carries and the console alone witnessed — a lease changing hands, a lease
    lapsing, an operator stopping a turn, and the operator's own prompt, which is accepted before
    it crosses any wire (`x/session_store.enqueue_prompt`) and so takes the `authored` arm and
    names no turn.
    """

    __tablename__ = "session_events"

    # Database-assigned, so the order events were written in survives a replica being replaced
    # mid-turn. Ordering by frame is coarser: several events come out of one frame.
    event_seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    # A projected event belongs to an exchange, because the fold only runs while a turn is open. An
    # authored one need not: a session that died before it ever reached a turn is the case that
    # category exists to record. It may still name one — `turn_aborted` does.
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session_turns.turn_id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[StoredEventKind] = mapped_column(
        TextBackedStrEnumUnionColumn(ConversationEventKind, AuthoredEventKind), nullable=False
    )
    # Which arm of the provenance union this row carries, and NOT NULL is the point: an event whose
    # origin is unstated is exactly what `session_messages` cannot rule out.
    provenance: Mapped[EventProvenance] = mapped_column(TextBackedStrEnumColumn(EventProvenance), nullable=False)
    # The inclusive span of frames this event was projected from — both set on the `frame_range`
    # arm and both NULL on `authored`, which the constraint below makes the only two possibilities.
    source_first_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_last_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The correlation key of a tool call's lifecycle, on both of its events and on nothing else, so
    # a call finds its answer by a lookup rather than by a parse of the frame log. Unique within a
    # session by the protocol's own contract, which is why it needs no per-message association.
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The event's remaining fields, in the neutral spelling; `x/session_events.py` is the one
    # place that reads or writes this shape.
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('message_completed','reasoning','tool_call_started',"
            "'tool_call_completed','prompt_enqueued','prompt_rejected','unreadable_input',"
            "'session_adopted','lease_expired','turn_aborted')",
            name="ck_session_events_kind",
        ),
        CheckConstraint("provenance IN ('frame_range','authored')", name="ck_session_events_provenance"),
        # The union, as the table states it: frames are present on exactly the `frame_range` arm, a
        # range has two ends or none, and an event projected from frames names the turn whose fold
        # produced it. One constraint because the clauses are one rule — the discriminator and the
        # columns it discriminates cannot disagree. The turn is required only on the `frame_range`
        # arm: the console authors facts about sessions that have no turn to name.
        CheckConstraint(
            "(provenance = 'frame_range') = (source_first_frame_seq IS NOT NULL) "
            "AND (source_first_frame_seq IS NULL) = (source_last_frame_seq IS NULL) "
            "AND (source_first_frame_seq IS NULL OR source_first_frame_seq <= source_last_frame_seq) "
            "AND (provenance <> 'frame_range' OR turn_id IS NOT NULL)",
            name="ck_session_events_provenance_frames",
        ),
        CheckConstraint(
            "(call_id IS NOT NULL) = (kind IN ('tool_call_started','tool_call_completed'))",
            name="ck_session_events_call_id",
        ),
        Index("idx_session_events_session", "session_id", "event_seq"),
    )


class SessionFrame(Base):
    """The agent's newline-delimited JSON protocol as it crossed the wire — and rows that are not
    that, which is a defect.

    The rollout — what the agent *did*, tool calls with their results — exists nowhere else.
    `session_messages` keeps an assistant message's `tool_use` blocks and not the frames carrying
    the results, so on its own it records every question and no answer.

    **The payload is the wire, not our parse of it.** Storing the SDK's dataclasses instead
    would silently inherit whatever the reader unpacks — thinking blocks are on the wire and
    are dropped by the turn loop's extraction, as is a result's cost and usage.

    **TODO(frame-vocabulary): this schema is in a half state and does not map to one concept.**
    ``kind`` holds two discriminator vocabularies, because two unrelated sinks write here:
    `RolloutRecorder` puts the CLI's own top-level ``type`` in it, and the setup reporter puts the
    *bridge* envelope's ``setup_output`` literal in it. So "what is this row" has two answers and
    neither field gives both, which is why there is no enum over ``kind``: one would name a concept
    this table does not have. The intended shape — the CLI's type in a column of its own — is
    <plans/conversation_layers.md> § 13.
    """

    __tablename__ = "session_frames"

    # Database-assigned so ordering needs no per-session counter in a process that can be
    # replaced mid-conversation. Gaps are expected and mean nothing; only the order does.
    frame_seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[FrameDirection] = mapped_column(TextBackedStrEnumColumn(FrameDirection), nullable=False)
    # The frame's own top-level `type`, lifted out so a reader can select `assistant` frames
    # without scanning JSONB.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The agent's own identity for this frame, where it has one — see
    # <../cli_protocol/frame_identity.py> for which kinds do and why deltas must not. NULL is the
    # common case and always will be, and means "no identity to compare" rather than "not yet
    # computed", so a reader must not treat two NULLs as the same frame.
    frame_uid: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The **runner's** number for this frame, off the bridge envelope (`ClaudeMessage.seq`), where
    # the runner gave one. Dense and monotonic over everything one runner process sent, which
    # `frame_seq` above is deliberately not — so this is the number a reconnect is computed from:
    # the console hands back the highest it holds and the runner replays only what is above it.
    #
    # NULL means no runner numbered this row: a frame this console wrote to the CLI, a
    # `setup_output` row the console authored itself, or a frame from a runner image predating the
    # field. Nothing reads it as the log's ordering — `frame_seq` still is.
    runner_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("direction IN ('to_agent','from_agent')", name="ck_session_frames_direction"),
        # The runner numbers what *it* puts on the wire, so a number on a frame this console sent
        # would be one nobody assigned.
        CheckConstraint(
            "runner_seq IS NULL OR direction = 'from_agent'", name="ck_session_frames_runner_seq_direction"
        ),
        Index("idx_session_frames_session", "session_id", "frame_seq"),
        # Reading a session by kind is otherwise a filter over its whole log, and the log holds
        # deltas — so the frame inspector, the narration read, and the MCP transcript fold would
        # each scan past them.
        Index("idx_session_frames_kind", "session_id", "kind", "frame_seq"),
        # What makes a replayed frame a no-op rather than a second line in the rollout. Partial
        # because most rows have no identity; a plain unique index would be almost all nulls.
        Index(
            "uq_session_frames_uid",
            "session_id",
            "frame_uid",
            unique=True,
            postgresql_where=text("frame_uid IS NOT NULL"),
        ),
        # What the resume cursor is read off: `max(runner_seq)` for one session, on every runner
        # connection. Partial because most rows have no runner number, and the reader always
        # excludes them.
        #
        # **Not unique, deliberately.** The insert can infer only one conflict target and today's
        # is `frame_uid`, so a second unique index would turn a replayed frame with no
        # agent-assigned identity (a `control_response`, a `system` without a `task_id`) from one
        # duplicate row into a raised `UniqueViolation` that ends the session. Uniqueness lands with
        # the release that moves the dedup onto position, not before.
        Index(
            "idx_session_frames_runner_seq", "session_id", "runner_seq", postgresql_where=text("runner_seq IS NOT NULL")
        ),
    )


class SessionOutbox(Base):
    """One reply a session produced, waiting for the room to be told it.

    **The row is the delivery**, written in the same transaction as the assistant message it is a
    copy of, and marked sent by the drain only once the send has returned — so a send that raises,
    or a replica that dies still holding a reply, is redriven rather than lost.

    **Replies only.** The console's narration — status line, lifecycle notices, holding and
    bootstrap lines — stays on `x/channels/matrix/pacer.py`'s in-process queue: what must never be
    lost silently is a reply the agent produced, and a notice that describes a moment is not worth
    redelivering minutes later. That is also why there is no `kind` column: every row here is a
    `REPLY`.

    **One target, one channel.** `room_id` is a Matrix room because that is the only channel
    there is; a second one joins by adding a discriminator beside it rather than by overloading
    this column (<plans/conversation_layers.md> § 5).
    """

    __tablename__ = "session_outbox"

    # Also the transaction id the send goes out under, so a redrive is refused by the homeserver
    # rather than posting twice — see `x/channels/matrix/outbox.py`'s `PendingReply.transaction_id`.
    outbox_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # The transcript row this reply is, which the room event states as its tag. NULL for a turn's
    # last word, which belongs to no message row and is keyed by `turn_id` below instead —
    # `result.result` on a turn whose completed messages were all empty. `_run_turn` otherwise
    # mints a message row for text that arrived only on a `result` frame.
    message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session_messages.message_id", ondelete="SET NULL"), nullable=True
    )
    agent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The idempotence key for the one reply a turn can produce that no transcript row holds: final
    # text its completed messages did not already queue. `_run_turn` writes it *before* closing the
    # turn, so a replica dying in that window leaves the turn open and its replacement re-derives
    # the same reply — which this makes a no-op rather than a second copy in the room. NULL
    # wherever `message_id` carries the identity instead.
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session_turns.turn_id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set once the homeserver has accepted the send, and never before it returns. Claimed, in
    # flight, refused, and dropped with its replica all leave this NULL, which is what makes
    # redrive the default rather than a case anyone has to detect.
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # When this row may be claimed again. Moved into the future by every claim, so a homeserver
    # that is refusing everything is retried on a widening interval instead of spending the row's
    # whole budget in a minute.
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # What the drain asks for on every pass: the oldest row for this room that nobody has
        # sent. Partial, because a sent row is the overwhelming majority and is never read again.
        Index("idx_session_outbox_unsent", "room_id", "created_at", postgresql_where=text("sent_at IS NULL")),
        # A message is queued for the room once. The assistant frame that completes a message can
        # be seen twice — a runner replaying its rollout into a replacement replica — and without
        # this the second sighting would queue a second copy of the same answer.
        Index("uq_session_outbox_message", "message_id", unique=True, postgresql_where=text("message_id IS NOT NULL")),
        # And a turn's last word is queued once, for the adoption case `turn_id` describes above.
        Index("uq_session_outbox_turn", "turn_id", unique=True, postgresql_where=text("turn_id IS NOT NULL")),
    )


class PushSubscription(Base):
    """One browser Push API subscription an Operator has granted this console.

    The ``endpoint`` a push service issues is both the address and the capability to push to that
    browser, so it is the identity: re-subscribing the same device overwrites its row rather than
    accumulating dead ones. ``p256dh`` and ``auth`` are that subscription's own RFC 8291 encryption
    inputs — the console encrypts each payload to them, so the push service relays ciphertext it
    cannot read.

    Subscriptions die silently and often. A push service reports that as 404/410, which prunes the
    row; ``last_failure_at`` records the transient failures that do not.
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


# Tables that exist in the database and in no ORM class. Dropping one takes the release after the
# one that stopped naming it, because `maxUnavailable: 0` keeps the previous image serving through
# the roll and that image selects every table it maps. `test_agent_authority_schema` excludes these
# from its ORM-versus-database comparison, which is otherwise exact.
UNMAPPED_TABLES_PENDING_DROP: frozenset[str] = frozenset()

# The same for `(table, column)` pairs in tables that stay. A separate set rather than an entry
# in the one above, which hides a whole table — naming `session_messages` there would stop the
# comparison noticing any drift in it.
UNMAPPED_COLUMNS_PENDING_DROP: frozenset[tuple[str, str]] = frozenset()

# Indexes the database has and no ORM class declares. Reachable only through a column above: an
# index over columns that are all still mapped would be drift rather than an unfinished drop.
UNMAPPED_INDEXES_PENDING_DROP: frozenset[str] = frozenset()


class MatrixAccessToken(Base):
    """The bot's access token, cached because Synapse rate-limits `/login`.

    One row per bot user, written from the pacer's queued send on whichever replica is speaking
    into the room. Absence means no token cached, so invalidating one is a `DELETE`, not a NULL.
    """

    __tablename__ = "matrix_access_token"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)


class MatrixSyncWatermark(Base):
    """Where the Matrix sync loop got to: everything before this has been acted on.

    One row per bot user, written by the sync pass on whichever replica holds the `MXSY` lock.
    Every pass writes it, because every pass finishes with what it read: a batch is handed to the
    session, or rejected and said so. No row means nothing has been finished with, so the loop
    reads from the beginning of the bot's timeline.
    """

    __tablename__ = "matrix_sync_watermark"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    next_batch: Mapped[str] = mapped_column(Text, nullable=False)


class MatrixConversation(Base):
    """The one room Haku services, and the chat session bound to it.

    CLEANUP(added 2026-08-17): superseded by `ChatAttachment` above, which holds the same binding
      keyed on the conversation instead of on the bot user. Unmap this class once nothing selects it
      — `rg MatrixConversation haku/` naming only this file — and drop the table a release after
      that unmapping has converged.

    Keyed by bot user rather than by room, which is what makes "one room at a time" a property of
    the schema instead of a rule the code has to remember: a second room cannot be recorded without
    displacing the first. That singleton is the thing on its way out, not a guarantee to build on —
    one bot serving several rooms is where this goes, and `ChatAttachment`'s partial unique index is
    the form of the rule that survives it.

    Separate from `matrix_sync_watermark` though both are singletons keyed the same way: separate
    rows keep a slow session claim from contending with the watermark write on every pass.
    """

    __tablename__ = "matrix_conversation"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The chat session currently serving the room. NULL between a session dying and the
    # supervisor replacing it — an expected state, not a broken one.
    #
    # **This is the pointer; `sessions.room_id` is the history.** This column says which session
    # the room is talking to *now* and moves every time the supervisor replaces one, while the
    # session's own `room_id` says which room that session served and never changes — which is
    # what keeps a past Matrix conversation findable after its successor has taken over. Asking
    # this column "was this session mine?" answers about the room's present instead, and reads as
    # no for every session but the live one.
    #
    # No constraint enforces that this points at a session whose `room_id` is this room: it is
    # an agreement between two rows, which SQL cannot state (a CHECK sees one row, and a
    # composite foreign key would need `room_id` in this table's key). It is maintained by
    # `MatrixSessionSupervisor` alone, and asserted by a test rather than by the database.
    session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="SET NULL"), nullable=True
    )
    joined_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MatrixRoomCursor(Base):
    """How far this room has been brought up to date with the conversation it holds a copy of.

    **The Matrix channel's own storage**, below the channel boundary and beside the outbox: the
    room holds a federated copy that outlives every console process, so after a restart the channel
    has to know what it already put there. A browser tab holds no such copy — several tabs can watch
    one conversation at different points and none of those positions should survive a refresh — so
    the SPA's position is a request parameter and there is deliberately no shared cursor table.

    Keyed by room, this channel's own address for a conversation — the same opacity
    `chat_attachment.address` records — and not by conversation, which would put the conversation
    layer's identity in the channel's private table for no reader.

    `event_seq` is a position in <x/subscription.py>'s stream, and an absent row is a room that has
    never read it — which is why the reader seeds it at the stream's head rather than at zero: a
    room serviced before this table existed already shows everything said in it.
    """

    __tablename__ = "matrix_room_cursor"

    room_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
