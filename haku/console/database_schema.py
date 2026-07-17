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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haku.console.agents.models import (
    MAX_AGENT_DISPLAY_NAME_LENGTH,
    AgentStatus,
    ClientRegistrationKind,
    CredentialBindingStatus,
    CredentialKind,
    EnrollmentPhase,
)
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


class McpOperatorOAuthAssociation(Base):
    __tablename__ = "mcp_operator_oauth_associations"
    __table_args__ = (
        UniqueConstraint("association_id", name="uq_mcp_operator_oauth_associations_association_id"),
        Index("idx_mcp_operator_oauth_associations_operator", "operator_id"),
    )

    server_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), primary_key=True
    )
    association_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid4, nullable=False)
    token_revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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

    One row per ``(operator_id, provider)`` — a single Google connection backs both the
    ``gmail`` and ``google_calendar`` in-process servers. The console self-refreshes
    ``access_token`` from ``refresh_token`` using the provider's fixed client (injected from
    Settings, never stored here); ``token_revision`` guards a concurrent refresh/reconnect.
    """

    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("connection_id", name="uq_provider_connections_connection_id"),
        Index("idx_provider_connections_operator", "operator_id"),
    )

    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[ProviderConnectionKind] = mapped_column(
        StringBackedStrEnumColumn(ProviderConnectionKind), primary_key=True
    )
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid4, nullable=False)
    token_revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderConnectionFlow(Base):
    """Short-lived authorization-code + PKCE flow state for a pending provider connection."""

    __tablename__ = "provider_connection_flows"
    __table_args__ = (
        Index("idx_provider_connection_flows_operator", "operator_id"),
        Index("idx_provider_connection_flows_expires_at", "expires_at"),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    operator_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operators.operator_id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[ProviderConnectionKind] = mapped_column(
        StringBackedStrEnumColumn(ProviderConnectionKind), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)


metadata = Base.metadata
