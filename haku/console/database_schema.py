"""SQLAlchemy ORM schema for haku-console's database."""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID, uuid4

# `ConversationItem` maps a column named `text`, which shadows the function inside that class body.
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    case,
    text,
    text as sql_text,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.hybrid import hybrid_property
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
    BridgeFrameKind,
    ChatSurface,
    ConversationEventKind,
    EventProvenance,
    FrameDirection,
    ItemStatus,
    ItemType,
    PromptOrigin,
    ReasoningDisclosure,
    RuntimeKind,
    SessionStatus,
    StoredEventKind,
    ToolOutcome,
    TurnOutcome,
)
from haku.console.grant_principal import GrantPrincipalKind
from haku.console.http_grant_models import HttpMethod, HttpMethods, HttpScheme
from haku.console.kubernetes_grant_models import KubernetesGrantScope, KubernetesGrantStatus, KubernetesRule
from haku.console.node_daemon_models import NodeDaemonExecutionStatus
from haku.console.operator_identity import OperatorStatus
from haku.console.provider_connection_registry import ProviderConnectionKind
from haku.console.pydantic_column import PydanticColumn
from haku.console.tool_calls import ToolCallStatus
from util.enum_vocab import UnknownValue
from util.sqlalchemy_types import (
    StrEnumColumn,
    StringBackedStrEnumColumn,
    TextBackedStrEnumColumn,
    TolerantTextBackedStrEnumUnionColumn,
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
            "access_profile_id IS NULL OR btrim(access_profile_id) <> ''",
            name="ck_enrollment_interactions_access_profile_id_nonempty",
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
    # Config-defined profile ID. NULL remains fail-closed during the expand/contract rollout.
    access_profile_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        CheckConstraint(
            "access_profile_id IS NULL OR btrim(access_profile_id) <> ''", name="ck_agents_access_profile_id_nonempty"
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
    access_profile_id: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class KubernetesGrantRow(Base):
    """One Agent-owned, principal-scoped, time-bounded Kubernetes capability lease.

    Scope and rules are intentionally JSONB: Kubernetes evolves its resource vocabulary, while
    the domain validates the stable namespace and RBAC-like shapes before writing.
    ``source_tool_call_id`` is retained as immutable provenance and must refer to the
    Agent-authenticated source call. Lifecycle ownership and authorization applicability are
    deliberately separate columns.
    """

    __tablename__ = "kubernetes_grants"
    __table_args__ = (
        CheckConstraint("btrim(source_tool_call_id) <> ''", name="ck_kubernetes_grants_source_tool_call_nonempty"),
        CheckConstraint(
            "jsonb_typeof(rules) = 'array' AND jsonb_array_length(rules) > 0",
            name="ck_kubernetes_grants_rules_nonempty",
        ),
        CheckConstraint(
            "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
            "AND principal_agent_id = owner_agent_id AND principal_session_id IS NULL) OR "
            "(principal_kind = 'session' AND principal_agent_id IS NULL "
            "AND principal_session_id IS NOT NULL)",
            name="ck_kubernetes_grants_principal_shape",
        ),
        CheckConstraint(
            "jsonb_typeof(scope) = 'object' "
            "AND scope ? 'kind' "
            "AND scope->>'kind' IN ('namespaces', 'all_namespaces', 'cluster', 'non_resource') "
            "AND ((scope->>'kind' = 'namespaces' "
            "AND scope ? 'namespaces' "
            "AND jsonb_typeof(scope->'namespaces') = 'array' "
            "AND jsonb_array_length(scope->'namespaces') > 0) "
            "OR (scope->>'kind' <> 'namespaces' AND NOT (scope ? 'namespaces')))",
            name="ck_kubernetes_grants_scope_shape",
        ),
        CheckConstraint("expires_at > created_at", name="ck_kubernetes_grants_expiration_after_creation"),
        CheckConstraint(
            "(status = 'active' AND ended_at IS NULL AND end_reason IS NULL) OR "
            "(status IN ('released', 'revoked', 'expired') AND ended_at IS NOT NULL "
            "AND end_reason IS NOT NULL AND btrim(end_reason) <> '')",
            name="ck_kubernetes_grants_status_shape",
        ),
        Index("idx_kubernetes_grants_source_tool_call", "source_tool_call_id"),
        Index("idx_kubernetes_grants_owner_status_expiry", "owner_agent_id", "status", "expires_at"),
        Index("idx_kubernetes_grants_agent_principal_status_expiry", "principal_agent_id", "status", "expires_at"),
        Index("idx_kubernetes_grants_session_principal_status_expiry", "principal_session_id", "status", "expires_at"),
    )

    grant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=False
    )
    principal_kind: Mapped[GrantPrincipalKind] = mapped_column(
        TextBackedStrEnumColumn(GrantPrincipalKind), nullable=False
    )
    principal_agent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=True
    )
    principal_session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="RESTRICT"), nullable=True
    )
    source_tool_call_id: Mapped[str] = mapped_column(
        Text, ForeignKey("mcp_tool_calls.tool_call_id", ondelete="RESTRICT"), nullable=False
    )
    scope: Mapped[KubernetesGrantScope] = mapped_column(PydanticColumn(KubernetesGrantScope), nullable=False)
    rules: Mapped[list[KubernetesRule]] = mapped_column(PydanticColumn(list[KubernetesRule]), nullable=False)
    status: Mapped[KubernetesGrantStatus] = mapped_column(
        TextBackedStrEnumColumn(KubernetesGrantStatus), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class HttpGrantRow(Base):
    """One Agent-owned, principal-scoped, time-bounded HTTP egress lease.

    The origin is three relational columns because a grant pins exactly ``(scheme, host, port)``;
    ``methods``/``path_regex`` narrow requests at that origin. The domain canonicalizes and
    validates coverage app-side (`http_grant_models`); Postgres holds only the relational
    invariants. Status is derived, never stored (root STYLE.md § SQLAlchemy): the row records the
    end facts — ``released_at``, ``revoked_at`` — and `http_grant_models.derive_status` computes
    the vocabulary from them and the clock, so expiry needs no sweeper.
    ``source_tool_call_id`` is retained as immutable provenance and must refer to the
    Agent-authenticated source call. Lifecycle ownership and authorization applicability are
    deliberately separate columns.
    """

    __tablename__ = "http_grants"
    __table_args__ = (
        CheckConstraint("btrim(source_tool_call_id) <> ''", name="ck_http_grants_source_tool_call_nonempty"),
        CheckConstraint(
            "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
            "AND principal_agent_id = owner_agent_id AND principal_session_id IS NULL) OR "
            "(principal_kind = 'session' AND principal_agent_id IS NULL "
            "AND principal_session_id IS NOT NULL)",
            name="ck_http_grants_principal_shape",
        ),
        CheckConstraint("expires_at > created_at", name="ck_http_grants_expiration_after_creation"),
        # The fact shape the derivation reads: at most one end action, and a reason exactly when
        # one is recorded.
        CheckConstraint(
            "num_nonnulls(released_at, revoked_at) <= 1 "
            "AND ((num_nonnulls(released_at, revoked_at) = 1) = (end_reason IS NOT NULL)) "
            "AND (end_reason IS NULL OR btrim(end_reason) <> '')",
            name="ck_http_grants_end_shape",
        ),
        CheckConstraint(
            "credential_handle IS NULL OR btrim(credential_handle) <> ''",
            name="ck_http_grants_credential_handle_nonempty",
        ),
        Index("idx_http_grants_source_tool_call", "source_tool_call_id"),
        Index("idx_http_grants_owner_expiry", "owner_agent_id", "expires_at"),
        Index("idx_http_grants_agent_principal_expiry", "principal_agent_id", "expires_at"),
        Index("idx_http_grants_session_principal_expiry", "principal_session_id", "expires_at"),
    )

    grant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=False
    )
    principal_kind: Mapped[GrantPrincipalKind] = mapped_column(
        TextBackedStrEnumColumn(GrantPrincipalKind), nullable=False
    )
    principal_agent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=True
    )
    principal_session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="RESTRICT"), nullable=True
    )
    source_tool_call_id: Mapped[str] = mapped_column(
        Text, ForeignKey("mcp_tool_calls.tool_call_id", ondelete="RESTRICT"), nullable=False
    )
    scheme: Mapped[HttpScheme] = mapped_column(TextBackedStrEnumColumn(HttpScheme), nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    methods: Mapped[frozenset[HttpMethod]] = mapped_column(PydanticColumn(HttpMethods), nullable=False)
    path_regex: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The handle is an inert config-registry name (`http_decide_config`); the credential value it
    # resolves to lives in a deployment env reference and never enters Postgres.
    credential_handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


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
        CheckConstraint(
            "session_id IS NULL OR binding_id IS NOT NULL", name="ck_mcp_tool_call_principals_session_agent"
        ),
        ForeignKeyConstraint(
            ["session_id", "binding_id"],
            ["sessions.session_id", "sessions.agent_binding_id"],
            name="fk_mcp_tool_call_principals_session_binding",
            ondelete="RESTRICT",
        ),
        Index("idx_mcp_tool_call_principals_operator_id", "operator_id"),
        Index("idx_mcp_tool_call_principals_binding_id", "binding_id"),
        Index("idx_mcp_tool_call_principals_session_id", "session_id"),
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
    session_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


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
    # Immutable durable identity selected when the conversation is opened.  The access profile is
    # denormalized intentionally: it is the profile the conversation was authorized under, not a
    # live lookup that replacement sessions may silently change.
    agent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=True
    )
    access_profile_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Immutable conversation identity. It lives here rather than on a session so a replacement
    # runner necessarily inherits the implementation whose prompt, replay and projection semantics
    # created the thread. Text + CHECK is deliberate: the next runtime widens one transactional
    # constraint rather than altering a PostgreSQL enum type.
    runtime_kind: Mapped[RuntimeKind] = mapped_column(TextBackedStrEnumColumn(RuntimeKind), nullable=False)
    # The next `conversation_event.event_seq` to hand out, taken under `SELECT … FOR UPDATE` in the
    # writing transaction. A counter here rather than a sequence because the log's address must be
    # **dense** — a sequence is unique but leaves gaps, and a gap a channel cannot distinguish from
    # loss is exactly what a position-based resume must not have.
    next_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "access_profile_id IS NULL OR btrim(access_profile_id) <> ''",
            name="ck_conversation_access_profile_id_nonempty",
        ),
        CheckConstraint("(agent_id IS NULL) = (access_profile_id IS NULL)", name="ck_conversation_agent_profile_pair"),
        CheckConstraint("runtime_kind IN ('claude_code', 'codex_app_server')", name="ck_conversation_runtime_kind"),
        CheckConstraint("next_event_seq > 0", name="ck_conversation_next_event_seq"),
        Index("idx_conversation_operator", "operator_id", "created_at"),
    )


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


class ChannelCursor(Base):
    """How far one attachment has read the conversation's log.

    **The only channel-generic piece of channel state.** A position in the log is what the
    conversation layer has to know about an attachment — it is the resume contract, and the same
    integer answers it for every channel. Everything else a channel keeps is its own rendering
    state, in its own tables, so a second channel is a new table rather than a widened shared one.

    Keyed by `attachment_id` rather than by the channel's own address, so a channel does not join
    its position to its deliveries through its public name. That does not make a browser tab
    durable: an attachment row exists only for a channel that holds a copy, so keying by attachment
    already excludes tabs.

    `event_seq` is a position in `conversation_event`, and an absent row is an attachment that has
    never read it — which is why the reader seeds it at the log's head rather than at zero: a room
    serviced before this table existed already shows everything said in it.
    """

    __tablename__ = "channel_cursor"

    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chat_attachment.attachment_id", ondelete="CASCADE"), primary_key=True
    )
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (CheckConstraint("event_seq >= 0", name="ck_channel_cursor_event_seq"),)


class Session(Base):
    """One Operator-owned agent conversation and its Agent Sandbox rendezvous.

    **`status` is derived, never stored** (root STYLE.md § SQLAlchemy): the row records the facts —
    allocation, attachment, the close request, the end — and the vocabulary every consumer speaks
    is computed from them in one place, the `status` hybrid below. Extending the vocabulary is
    therefore a fact change, and the decision-value roll rule (<../README.md> § Vocabularies across
    a roll) lands on the derivation: the release that derives a new member from a new fact column
    ships that derivation one release before anything writes the fact, because an older replica's
    derivation reads the row as whichever old member its facts spell.
    """

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
    # The exact credential generation that authorized this sandbox session. A replacement may use
    # a successor binding while retaining the conversation's immutable Agent/profile identity.
    agent_binding_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("credential_bindings.binding_id", ondelete="RESTRICT"), nullable=True
    )
    # The verifier for this session's sandbox credential — SHA-256 of a bearer minted once at
    # allocation and never stored. The runner presents it on every rendezvous and the sandbox Agent
    # presents it to Console MCP; both resolve to this exact session. Admissibility is the status,
    # live connection/lease and pinned Agent authority's business, while the SandboxClaim is
    # `claim_cleaned_at`'s.
    # Absent when the session has never owned a sandbox: initially while idle, and still absent if
    # that session closes or fails before allocation. Allocation mints the session credential and
    # stores its fingerprint in the same transaction that starts provisioning.
    bridge_token_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
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
    # A bound like `conversation_turn.first_frame_seq`, not a pointer: frame numbering leaves gaps,
    # and a turn opens anchored at the frame before it, which is a value no row has.
    # **`0` is "nothing here has ever projected"**, which no frame's `frame_seq` can be, so the
    # bound needs no absent state; it arrives by the server default.
    projected_frame_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # The operator asked this session to end. Stamped once and never cleared — ending is one-way —
    # which is what derives `closing` until `ended_at` lands.
    close_requested_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The session is over as of this instant, whatever ended it; NULL is a session that can still
    # run. With `error` it spells the terminal member: ended with an error is `failed`, without one
    # `closed`.
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why it ended badly. NULL until `ended_at`, and NULL forever on a clean close
    # (`ck_sessions_error_ended`).
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Renewed by whichever replica currently holds this session's runner websocket: a replica that
    # dies mid-turn otherwise leaves the row claiming a turn is in flight forever. Absent only while
    # idle (or if that unallocated session ends); allocation installs the provisioning grant in the
    # transaction that mints the sandbox session credential.
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Which replica is asserting the lease, and — by being NULL or not — which of the lease's two
    # meanings is running. NULL is the creator's provisioning grant: nobody holds this session
    # yet, it is merely budgeted until a runner attaches. Set means that pod's heartbeat, so an
    # expired lease names the process to go read; `HOSTNAME` is the pod name `kubectl logs` takes.
    # Permanently nullable: "no holder yet" is a real state.
    lease_holder: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("bridge_token_fingerprint", name="uq_sessions_bridge_token_fingerprint"),
        UniqueConstraint("session_id", "agent_binding_id", name="uq_sessions_session_agent_binding"),
        # The fact shapes the derivation reads, held by the database so no writer can record a
        # combination the vocabulary cannot say. An error is how an ended session ended;
        CheckConstraint("error IS NULL OR ended_at IS NOT NULL", name="ck_sessions_error_ended"),
        # a runner can only ever have attached to a session that was allocated a credential;
        CheckConstraint(
            "bridge_connected_at IS NULL OR bridge_token_fingerprint IS NOT NULL",
            name="ck_sessions_connected_allocated",
        ),
        # and while a session can still run, allocation and its lease arrive together — idle holds
        # neither, an allocated session both, so "live but unreclaimable" is unwritable.
        CheckConstraint(
            "ended_at IS NOT NULL OR close_requested_at IS NOT NULL "
            "OR ((bridge_token_fingerprint IS NULL) = (lease_expires_at IS NULL))",
            name="ck_sessions_allocation_lease",
        ),
        # Claim cleanup is only ever recorded against a session that has ended.
        CheckConstraint("claim_cleaned_at IS NULL OR ended_at IS NOT NULL", name="ck_sessions_claim_cleanup_ended"),
        Index("idx_sessions_operator", "operator_id", "created_at"),
        Index("idx_sessions_conversation", "conversation_id", "created_at"),
        Index(
            "idx_sessions_expired_lease",
            "lease_expires_at",
            postgresql_where=text(
                "ended_at IS NULL AND close_requested_at IS NULL AND bridge_token_fingerprint IS NOT NULL"
            ),
        ),
    )

    @hybrid_property
    def status(self) -> SessionStatus:
        """The lifecycle vocabulary, derived from the row's facts at the moment of asking.

        One derivation for Python reads and, via the expression below, SQL filters — so no writer
        maintains a summary that could disagree with the facts it summarizes. `RESPONDING` is
        deliberately not derived here: whether a turn is open is `conversation_turn`'s fact, and
        `conversation_views.live_status` layers it on top of this member.
        """
        if self.ended_at is not None:
            return SessionStatus.FAILED if self.error is not None else SessionStatus.CLOSED
        if self.close_requested_at is not None:
            return SessionStatus.CLOSING
        if self.bridge_token_fingerprint is None:
            return SessionStatus.IDLE
        if self.bridge_connected_at is None:
            return SessionStatus.PROVISIONING
        return SessionStatus.READY

    @status.inplace.expression
    @classmethod
    def _status_expression(cls) -> ColumnElement[SessionStatus]:
        # `type_coerce` rather than a bare CASE so a selected status decodes to the enum, and
        # labelled so `select(Session.status, …)` rows answer `.status` by name — both exactly as
        # the stored column did. In a WHERE the label compiles to its element, so filters are
        # untouched by it.
        return type_coerce(
            case(
                (cls.ended_at.is_not(None) & cls.error.is_not(None), SessionStatus.FAILED),
                (cls.ended_at.is_not(None), SessionStatus.CLOSED),
                (cls.close_requested_at.is_not(None), SessionStatus.CLOSING),
                (cls.bridge_token_fingerprint.is_(None), SessionStatus.IDLE),
                (cls.bridge_connected_at.is_(None), SessionStatus.PROVISIONING),
                else_=SessionStatus.READY,
            ),
            TextBackedStrEnumColumn(SessionStatus),
        ).label("status")


class ConversationEvent(Base):
    """The record. Every fact about a conversation is written here, once.

    Everything else in this file's chat half is either derived from these rows or belongs to a
    layer below. What it replaces kept a second, independently written account of the same facts —
    a transcript table the fold updated in place — so the two could disagree and nothing said which
    was right. Here the log is the only writer's target and the entities are folds of it.

    **The address is dense within one conversation**, which is what makes a position sufficient for
    a channel: a gap is evidence of loss rather than an artifact of a shared sequence, and "the next
    one after N" is answerable. `conversation.next_event_seq` is taken under `SELECT … FOR UPDATE`
    in the writing transaction — one row lock per write, affordable because segments are coalesced
    (a turn writes tens of rows, not thousands) and only one session holds a conversation at a time.

    **Prose exists only as segments, and a completion carries none.** `ConversationEventKind` states
    the invariant; what it buys here is that `conversation_item.text` is a fold of these rows rather
    than a column some other writer maintains.

    The provenance union is unchanged from what it replaces: frames are present on exactly the
    `frame_range` arm, a range has two ends or none, and a frame-derived row names both a turn and a
    session. What is added is that a frame-derived row must also name its item, so a rebuild can
    find what the frames produced.
    """

    __tablename__ = "conversation_event"

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), primary_key=True
    )
    # Dense within the conversation, from 1. Not `Identity`: a database-assigned sequence is unique
    # but not gapless, and a channel resuming at a position needs to tell "nothing since" from
    # "something I did not receive".
    event_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # NULL for a fact the conversation holds that no session has taken — a prompt accepted before a
    # sandbox exists. A frame-derived row always names one, which the constraint below states.
    session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=True
    )
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_turn.turn_id", ondelete="CASCADE"), nullable=True
    )
    # The item this row is about, absent only on the rows that are about no item: a turn's two ends
    # and the session lifecycle facts.
    item_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_item.item_id", ondelete="CASCADE"), nullable=True
    )
    # Narration, so it decodes tolerantly: a reader that has no words for a kind a newer replica
    # wrote passes over it, which is the correct behaviour for an append-only stream rather than a
    # degradation (<README.md> § Vocabularies across a roll).
    kind: Mapped[StoredEventKind | UnknownValue] = mapped_column(
        TolerantTextBackedStrEnumUnionColumn(ConversationEventKind, AuthoredEventKind), nullable=False
    )
    provenance: Mapped[EventProvenance] = mapped_column(TextBackedStrEnumColumn(EventProvenance), nullable=False)
    source_first_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_last_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The row's remaining fields, in the neutral spelling; `x/session_events.py` is the one place
    # that reads or writes this shape.
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_seq > 0", name="ck_conversation_event_seq_positive"),
        CheckConstraint("provenance IN ('frame_range','authored')", name="ck_conversation_event_provenance"),
        # The union, as the table states it. One constraint because the clauses are one rule: the
        # discriminator and the columns it discriminates cannot disagree.
        CheckConstraint(
            "(provenance = 'frame_range') = (source_first_frame_seq IS NOT NULL) "
            "AND (source_first_frame_seq IS NULL) = (source_last_frame_seq IS NULL) "
            "AND (source_first_frame_seq IS NULL OR source_first_frame_seq <= source_last_frame_seq) "
            "AND (provenance <> 'frame_range' OR turn_id IS NOT NULL) "
            "AND (provenance <> 'frame_range' OR session_id IS NOT NULL) "
            "AND (provenance <> 'frame_range' OR item_id IS NOT NULL)",
            name="ck_conversation_event_provenance_frames",
        ),
        # The three item kinds are about an item and the rest are not. This is what replaces the
        # constraint that pinned kinds to the `frame_range` arm: an item kind may take either arm —
        # a prompt is authored, an assistant message is folded — so the arm follows from
        # `conversation_item.item_type`, which this table cannot see, and the kind states only
        # whether an item is named at all.
        CheckConstraint(
            "(item_id IS NOT NULL) = (kind IN ('item_started','item_segment','item_completed'))",
            name="ck_conversation_event_item_kinds",
        ),
        # How a channel reads its thread: everything after a position. The primary key already
        # serves it, so what this adds is the session-scoped read the frame appeal path uses.
        Index("idx_conversation_event_session", "session_id", "event_seq"),
        Index("idx_conversation_event_item", "item_id", "event_seq"),
    )


# A nullable JSONB column where `None` means the field is **absent**, not that its value is JSON
# `null`. SQLAlchemy's default is the other reading — it writes the two-byte document `null` — which
# every `IS NULL` check on `conversation_item` then fails, and which no reader could tell from a
# provider that genuinely sent `null`.
_ABSENT_JSONB = JSONB(none_as_null=True)


class ConversationItem(Base):
    """One thing in the transcript — a prompt, a message, a reasoning block, a tool call.

    Derived wholly from the log, and asserted to be: re-folding `conversation_event` reproduces
    every column here. What that buys is `text = concat(segments)` as a checkable invariant rather
    than a hope, and it is why the two paths that used to write a transcript row with no log row
    behind them cannot exist.

    **`status` is the item's lifecycle and nothing else.** The enum it replaces put a prompt's queue
    state and an answer's completeness in one column, told apart only by `role`; the queue state is
    `conversation_prompt`'s now.

    **A tool call's arguments are complete or the call is not started.** Two of three backends stream
    arguments as partial JSON, so `arguments` is written from the `.done` and "a call is being
    composed" is deliberately not expressible — a channel learns of a call when there is something
    true to say about it.
    """

    __tablename__ = "conversation_item"

    item_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False
    )
    # Absent on a prompt no session has claimed.
    session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=True
    )
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_turn.turn_id", ondelete="CASCADE"), nullable=True
    )
    item_type: Mapped[ItemType] = mapped_column(TextBackedStrEnumColumn(ItemType), nullable=False)
    status: Mapped[ItemStatus] = mapped_column(TextBackedStrEnumColumn(ItemStatus), nullable=False)
    opened_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    closed_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The concatenation of this item's segments in `event_seq` order. A materialisation of the log
    # and never a second authority for it: `x/reprojection.py` asserts the two agree.
    item_text: Mapped[str] = mapped_column("text", Text, nullable=False)
    # What the backend called this item. Provenance, never identity — Claude Code omits it on many
    # rows and on every delta, which is why the console mints its own.
    backend_item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Prompt only: which attachment or surface sent it.
    origin: Mapped[PromptOrigin | None] = mapped_column(PydanticColumn(PromptOrigin), nullable=True)
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    arguments: Mapped[dict[str, Any] | None] = mapped_column(_ABSENT_JSONB, nullable=True)
    outcome: Mapped[ToolOutcome | None] = mapped_column(TextBackedStrEnumColumn(ToolOutcome), nullable=True)
    # The per-tool result payload, behind `Json` on purpose: a channel rendering a shell result's
    # `exitCode` knows shell commands, not one backend. Typed as any JSON value rather than an
    # object, because it is one — production sends lists of blocks and bare strings here too.
    structured: Mapped[Any | None] = mapped_column(_ABSENT_JSONB, nullable=True)
    disclosure: Mapped[ReasoningDisclosure | None] = mapped_column(
        TextBackedStrEnumColumn(ReasoningDisclosure), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("item_type IN ('prompt','message','reasoning','tool_call')", name="ck_conversation_item_type"),
        CheckConstraint("status IN ('open','complete','failed')", name="ck_conversation_item_status"),
        # Open holds exactly while the item has no closing position, so "is this still streaming"
        # has one answer rather than two that can disagree.
        CheckConstraint("(status = 'open') = (closed_seq IS NULL)", name="ck_conversation_item_open"),
        CheckConstraint("closed_seq IS NULL OR closed_seq >= opened_seq", name="ck_conversation_item_close_after_open"),
        # The per-type fields, stated against the type that owns them.
        CheckConstraint(
            "(item_type = 'tool_call') = (call_id IS NOT NULL) "
            "AND (item_type = 'tool_call') = (tool_name IS NOT NULL) "
            "AND (item_type = 'tool_call' OR arguments IS NULL) "
            "AND (item_type = 'tool_call' OR outcome IS NULL) "
            "AND (item_type = 'tool_call' OR structured IS NULL)",
            name="ck_conversation_item_tool_call_fields",
        ),
        CheckConstraint(
            "(item_type = 'reasoning' OR disclosure IS NULL) AND (item_type = 'prompt' OR origin IS NULL)",
            name="ck_conversation_item_typed_fields",
        ),
        # A terminal state carries its type's terminal field, so a closed call always says how it
        # went and a closed reasoning item always says what it disclosed.
        CheckConstraint(
            "status <> 'complete' OR ((item_type <> 'tool_call' OR outcome IS NOT NULL) "
            "AND (item_type <> 'reasoning' OR disclosure IS NOT NULL))",
            name="ck_conversation_item_complete_terminal_fields",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded','failed','unknown')", name="ck_conversation_item_outcome"
        ),
        CheckConstraint(
            "disclosure IS NULL OR disclosure IN ('summary','withheld')", name="ck_conversation_item_disclosure"
        ),
        # A call id answers once within its conversation, which is what lets a result find its call
        # by lookup rather than by a parse of the frame log.
        Index(
            "uq_conversation_item_call",
            "conversation_id",
            "call_id",
            unique=True,
            postgresql_where=sql_text("call_id IS NOT NULL"),
        ),
        Index("idx_conversation_item_conversation", "conversation_id", "opened_seq"),
        Index("idx_conversation_item_turn", "turn_id", "opened_seq"),
        # The `read_items` keyset branches: a page of entries is served from the rows' defining
        # stream positions, so each branch needs an index that already stands in that order —
        # partial, because the branch's filter would otherwise make it a scan of the other rows.
        Index(
            "idx_conversation_item_tool_call_opened",
            "conversation_id",
            "opened_seq",
            postgresql_where=sql_text("item_type = 'tool_call'"),
        ),
        Index(
            "idx_conversation_item_completed",
            "conversation_id",
            "closed_seq",
            postgresql_where=sql_text("status = 'complete'"),
        ),
    )


class ConversationTurn(Base):
    """One exchange, derived from the log's `turn_started` and `turn_ended`.

    **One open turn per conversation, not per session.** "Only one session holds a conversation at a
    time" is a conversation-layer rule, so the index enforcing it belongs on the conversation.

    The columns a turn no longer carries were the turn loop's own scratch state. Which message a
    turn is streaming into is the item of this turn that is still open; whether it said anything is
    whether it has a completed `message` item; whether a reply is queued is delivery state and
    belongs to the channel that owes it.
    """

    __tablename__ = "conversation_turn"

    turn_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    first_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Bounds into this session's wire log, so an operator can appeal the folded text to the frames.
    # Absent on a turn that opened or closed on no frame at all.
    first_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_frame_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[TurnOutcome | None] = mapped_column(TextBackedStrEnumColumn(TurnOutcome), nullable=True)
    # Set exactly when `outcome` is `failed`, and the check below is what makes that true rather
    # than hoped: a failed turn that states no reason is the state #4752 was filed about.
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','aborted','failed')", name="ck_conversation_turn_outcome"
        ),
        # A turn is ended exactly when it says how it went, so "is this exchange running" cannot be
        # read two ways.
        CheckConstraint("(ended_at IS NULL) = (outcome IS NULL)", name="ck_conversation_turn_ended"),
        CheckConstraint("(failure IS NULL) = (outcome IS DISTINCT FROM 'failed')", name="ck_conversation_turn_failure"),
        CheckConstraint("(ended_at IS NULL) = (last_seq IS NULL)", name="ck_conversation_turn_last_seq"),
        CheckConstraint("last_seq IS NULL OR last_seq >= first_seq", name="ck_conversation_turn_seq_order"),
        Index(
            "uq_conversation_turn_open", "conversation_id", unique=True, postgresql_where=sql_text("ended_at IS NULL")
        ),
        Index("idx_conversation_turn_conversation", "conversation_id", "first_seq"),
        Index("idx_conversation_turn_session", "session_id", "first_seq"),
        # The `read_items` turn-end branch, ordered by where each ended exchange closed.
        Index(
            "idx_conversation_turn_ended",
            "conversation_id",
            "last_seq",
            postgresql_where=sql_text("last_seq IS NOT NULL"),
        ),
    )


class ConversationPrompt(Base):
    """A prompt waiting to be asked, and which session took it.

    **Keyed by the conversation, so a prompt may precede a runner.** Admission is a
    conversation-layer decision and a session claims a prompt once one exists, so a prompt sent to a
    thread whose sandbox has not been provisioned is a queued row rather than a refusal nothing can
    record.

    `turn_id` is nullable and many prompts may name one turn, which is `turn/steer` and Claude
    Code's mid-turn fold said once. It replaces a join table: many prompts naming one turn is the
    same relation with one fewer row to keep consistent.

    **The prompt's text is not here.** It is the `prompt` item's, so the queue and the transcript
    cannot come to disagree about what was asked.
    """

    __tablename__ = "conversation_prompt"

    prompt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_item.item_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_turn.turn_id", ondelete="SET NULL"), nullable=True
    )
    queued_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by_session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("(claimed_at IS NULL) = (claimed_by_session_id IS NULL)", name="ck_conversation_prompt_claim"),
        CheckConstraint(
            "claimed_at IS NULL OR claimed_at >= queued_at", name="ck_conversation_prompt_claim_after_queue"
        ),
        # One unclaimed prompt per conversation: admission is what decides whether a second may be
        # accepted, and it decides against a thread that already owes an answer.
        Index(
            "uq_conversation_prompt_unclaimed",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("claimed_at IS NULL"),
        ),
        Index("idx_conversation_prompt_conversation", "conversation_id", "queued_at"),
    )


class SessionFrame(Base):
    """The bridge's opaque wire log for one session.

    The rollout — what the agent *did*, tool calls with their results — exists nowhere else.
    `conversation_item` keeps a tool call's arguments and not the frames carrying the results, so
    on its own it records every question and no answer.

    **The payload is the wire, not our parse of it.** Storing the SDK's dataclasses instead
    would silently inherit whatever the reader unpacks — thinking blocks are on the wire and
    are dropped by the turn loop's extraction, as is a result's cost and usage.

    ``kind`` is only the bridge class (``harness_frame`` or ``setup_output``). For harness rows,
    ``payload`` stores the complete native frame exactly as the selected harness emitted or
    received it, including its own ``type`` or JSON-RPC method when it has one; none is copied into
    this column and no provider wrapper is added.
    """

    __tablename__ = "session_frames"

    # Database-assigned so ordering needs no per-session counter in a process that can be
    # replaced mid-conversation. Gaps are expected and mean nothing; only the order does.
    frame_seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[FrameDirection] = mapped_column(TextBackedStrEnumColumn(FrameDirection), nullable=False)
    # The outer bridge discriminator, never the native payload's `type` or JSON-RPC method.
    kind: Mapped[BridgeFrameKind] = mapped_column(TextBackedStrEnumColumn(BridgeFrameKind), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The **runner's** number for this frame, off the bridge envelope (`HarnessFrame.seq`), where
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
        CheckConstraint("kind IN ('harness_frame','setup_output')", name="ck_session_frames_kind"),
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
        # What the resume cursor is read off: `max(runner_seq)` for one session, on every runner
        # connection. Partial because most rows have no runner number, and the reader always
        # excludes them.
        #
        Index(
            "uq_session_frames_runner_seq",
            "session_id",
            "runner_seq",
            unique=True,
            postgresql_where=text("runner_seq IS NOT NULL"),
        ),
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
# in the one above, which hides a whole table — naming `conversation_item` there would stop the
# comparison noticing any drift in it.
UNMAPPED_COLUMNS_PENDING_DROP: frozenset[tuple[str, str]] = frozenset(
    {("agents", "auto_approval_policy"), ("enrollment_interactions", "auto_approval_policy")}
)

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


class MatrixRevision(Base):
    """Which homeserver event the Matrix channel is currently editing for a revisable subject.

    **This is the channel's, not the conversation's.** It is what `chat_delivery` becomes once
    narrowed to what it was actually read for: the subjects Matrix can *revise* — a status line it
    edits and retires — against the event ids it edits them at. Nothing outside Matrix reads it, and
    a channel that cannot edit what it sent has no use for the shape.

    A row per delivered message is a flushed-up-to position materialised one row at a time, and
    `channel_cursor` holds that properly. So only revisable subjects are here.

    Both columns are opaque outside this channel, as `chat_attachment.address` is: `event_id` is
    where the channel put it and `subject` is what it decided to show there.

    **Live means the channel still shows it.** `retired_at` is set when the channel takes it back,
    and `uq_matrix_revision_live_subject` is what makes re-deriving a subject find the event already
    showing it instead of sending a second one. A retired row stays: the channel's copy cannot be
    asked what it used to show.
    """

    __tablename__ = "matrix_revision"

    revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chat_attachment.attachment_id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("btrim(subject) <> ''", name="ck_matrix_revision_subject_nonempty"),
        CheckConstraint("btrim(event_id) <> ''", name="ck_matrix_revision_event_nonempty"),
        CheckConstraint("retired_at IS NULL OR retired_at >= sent_at", name="ck_matrix_revision_retire_after_sent"),
        Index(
            "uq_matrix_revision_live_subject",
            "attachment_id",
            "subject",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
    )


class MatrixRoomCopy(Base):
    """One Haku-authored room event whose tag names the conversation event it projects.

    **The room's copy, as durable correspondence.** Written by the sync loop from the events'
    own `/sync` echoes — never by the send path — and read by the room's reconciler before it
    sends: a source already showing in the room is not sent again, however long ago the send was,
    which is what outlives Synapse's 30-to-60-minute transaction cache. Nothing outside the Matrix
    channel reads it.

    `redacted` marks a copy the room no longer shows. The row stays: correspondence answers "did
    this projection reach the room", and a redaction does not unsend — it only removes the copy
    from duplicate repair, which considers live originals alone.

    `replaces_event_id` marks an `m.replace` revision of an earlier event. An edit is a content
    change to a copy the room already shows, so it satisfies correspondence without ever being a
    second copy to repair.
    """

    __tablename__ = "matrix_room_copy"

    # The homeserver's id for the event, globally unique by construction.
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # The attachment named by the event's own tag: a rebound room's old events keep naming the
    # attachment they were projected under, so the new attachment starts with no correspondence.
    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chat_attachment.attachment_id", ondelete="CASCADE"), nullable=False
    )
    source_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    replaces_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The homeserver's ordering, which is what decides the copy to keep when repairing duplicates.
    origin_server_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        CheckConstraint("btrim(event_id) <> ''", name="ck_matrix_room_copy_event_nonempty"),
        CheckConstraint("source_event_seq > 0", name="ck_matrix_room_copy_source_positive"),
        # What both readers ask: does this source show, and which copies share it.
        Index("idx_matrix_room_copy_source", "attachment_id", "source_event_seq"),
    )


class MatrixOutbox(Base):
    """One thing the channel owes its room, waiting to be sent.

    `session_outbox` with the session removed: keyed by the attachment, because what owes the room
    is the channel holding the copy and not whichever session happened to produce the words. It
    stays a durable queue rather than becoming a position, because retry state against a flaky
    homeserver is real state and a position cannot express "this one failed three times and is
    backing off".

    **Written by the channel and never by a turn.** The turn writes the log and stops; the channel
    reads its cursor forward and decides what to send. That is the change this table's shape
    records — its predecessor was written inside the turn's transaction, which is what tied the
    conversation's writer to one channel's address.

    **`subject` is the idempotence key**, in the same opaque channel vocabulary as
    `matrix_revision.subject`. It replaces the three-way `message_id`/`turn_id`/`agent_message_id`
    identity the old table needed, which existed because a turn could produce a reply no transcript
    row held. Prose is only ever segments of an item now, so that case is gone.
    """

    __tablename__ = "matrix_outbox"

    # Also the transaction id the send goes out under, so a redrive is refused by the homeserver
    # rather than posting twice.
    outbox_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chat_attachment.attachment_id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
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
        CheckConstraint("btrim(subject) <> ''", name="ck_matrix_outbox_subject_nonempty"),
        # What the drain asks for on every pass: the oldest row for this attachment that nobody has
        # sent. Partial, because a sent row is the overwhelming majority and is never read again.
        Index("idx_matrix_outbox_unsent", "attachment_id", "created_at", postgresql_where=text("sent_at IS NULL")),
        # A subject is queued once. The event that produces it can be seen twice — a runner
        # replaying its rollout into a replacement replica — and without this the second sighting
        # would queue a second copy of the same answer.
        Index("uq_matrix_outbox_subject", "attachment_id", "subject", unique=True),
    )


class MatrixIngressEvent(Base):
    """Which prompt in the record carries an inbound Matrix event.

    The dedupe key for ingress, and the Matrix channel's own table: an `event_id` is this
    channel's address for a message and nothing above the channel boundary reads it.

    **A row is written in the prompt's own transaction** (`session_store.enqueue_prompt`'s
    `records` hook), which is the whole point of the table. The watermark commits separately and
    afterwards, so a crash between the two re-delivers a batch the session already holds; a row
    written beside the watermark instead would be missing in exactly that case.

    **Presence therefore means the record carries the event, not that the loop saw it.** That is
    what makes suppressing a re-delivered event safe: the prompt is in the transcript, queued on the
    conversation rather than on the session that accepted it, so whichever session runs next claims
    it.

    Rejected and unreadable events are deliberately absent: both are recorded in the transaction
    that advances the watermark, so a crash before it leaves neither the acknowledgement nor the
    row, and the re-delivery is a clean first offer.
    """

    __tablename__ = "matrix_ingress_event"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # The `prompt` item this event became. A prompt is an item like any other now, so this points
    # at the transcript rather than at a separate message table.
    item_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_item.item_id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (Index("idx_matrix_ingress_event_item", "item_id"),)
