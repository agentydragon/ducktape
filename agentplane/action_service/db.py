"""The Action Service's Postgres schema: its rows, the notifications their commits send, and the engine."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.models import (
    ActionState,
    ExecutionState,
    ExternalGrantProvenance,
    PolicyEvidence,
    Verdict,
)
from agentplane.action_service.updates import CHANNEL, CONNECTIONS_CHANNEL, DISPATCH_CHANNEL, MCP_LINKAGE_CHANNEL
from agentplane.subjects import ServiceAccountRef
from util.sqlalchemy_types import PydanticColumn, TextBackedStrEnumColumn

# SQLAlchemy loads asyncpg from the URL scheme; Gazelle cannot infer that runtime dependency.
# gazelle:include_dep @pypi//asyncpg

SessionMaker = async_sessionmaker[AsyncSession]


class Base(DeclarativeBase):
    pass


class EnrollmentRow(Base):
    __tablename__ = "connection_enrollment"
    __table_args__ = (UniqueConstraint("client_id", "redirect_uri", "code_challenge"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    handle_hash: Mapped[str] = mapped_column(Text, unique=True)
    issuer: Mapped[str] = mapped_column(Text)
    client_id: Mapped[str] = mapped_column(Text)
    client_name: Mapped[str | None] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(Text)
    upstream_url: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer)
    browser_hash: Mapped[str | None] = mapped_column(Text)
    operator_issuer: Mapped[str | None] = mapped_column(Text)
    operator_subject: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[Verdict | None] = mapped_column(TextBackedStrEnumColumn(Verdict))
    decision_digest: Mapped[str | None] = mapped_column(Text)
    # Set by an allow decision.
    caller: Mapped[ServiceAccountRef | None] = mapped_column(PydanticColumn(ServiceAccountRef))
    display_name: Mapped[str | None] = mapped_column(Text)
    connection_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("external_connection.id"))
    connection_version: Mapped[int | None] = mapped_column(Integer)
    exchange_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectionRow(Base):
    __tablename__ = "external_connection"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    bound_caller: Mapped[ServiceAccountRef | None] = mapped_column(PydanticColumn(ServiceAccountRef), nullable=True)
    binding_version: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectionGrantRow(Base):
    __tablename__ = "external_connection_grant"
    __table_args__ = (UniqueConstraint("connection_id", "revision"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("external_connection.id"))
    revision: Mapped[int] = mapped_column(Integer)
    # Immutable authorization evidence, independent of the current Connection binding.
    original_caller: Mapped[ServiceAccountRef] = mapped_column(PydanticColumn(ServiceAccountRef))
    issuer: Mapped[str] = mapped_column(Text)
    client_id: Mapped[str] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(Text)
    activation_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(ConnectionRow, "after_insert")
@event.listens_for(ConnectionRow, "after_update")
@event.listens_for(ConnectionGrantRow, "after_insert")
@event.listens_for(ConnectionGrantRow, "after_update")
def _notify_connection(_mapper: Mapper[object], connection: Connection, _row: object) -> None:
    # This listener sees every canonical inventory mutation, including OAuth adapter writes.
    connection.execute(select(func.pg_notify(CONNECTIONS_CHANNEL, "")))


class ActionRequestRow(Base):
    __tablename__ = "action_request"
    __table_args__ = (
        UniqueConstraint("caller_namespace", "caller_name", "idempotency_key"),
        Index("ix_action_request_caller_created", "caller_namespace", "caller_name", "created_at"),
        Index("ix_action_request_state_created", "state", "created_at"),
        Index(
            "action_request_history_page",
            text("history_at DESC"),
            text("id DESC"),
            postgresql_where=text("history_at IS NOT NULL"),
        ),
        Index(
            "action_request_pending_page",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("state = 'decision_pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(Text)
    action: Mapped[ActionIdentity] = mapped_column(PydanticColumn(ActionIdentity))
    arguments: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    caller_namespace: Mapped[str] = mapped_column(Text)
    caller_name: Mapped[str] = mapped_column(Text)
    external_grant: Mapped[ExternalGrantProvenance | None] = mapped_column(PydanticColumn(ExternalGrantProvenance))
    state: Mapped[ActionState] = mapped_column(TextBackedStrEnumColumn(ActionState))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionEventRow(Base):
    __tablename__ = "action_event"

    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[ActionState] = mapped_column(TextBackedStrEnumColumn(ActionState))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Either kind of principal, and nothing queries by it, so it keeps its own shape.
    actor: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))


@event.listens_for(ActionEventRow, "after_insert")
def _notify_event(_mapper: Mapper[ActionEventRow], connection: Connection, row: ActionEventRow) -> None:
    # NOTIFY is delivered only on commit, including for writers in other service replicas.
    # Keep this on canonical event insertion so every lifecycle transition wakes readers.
    connection.execute(select(func.pg_notify(CHANNEL, str(row.request_id))))


class DecisionRow(Base):
    __tablename__ = "action_decision"
    __table_args__ = (
        UniqueConstraint("request_id"),
        # A DecisionProvider decided when both operator columns are NULL, and Postgres counts NULLs
        # as distinct by default -- which would exempt exactly those rows from the replay backstop.
        UniqueConstraint(
            "provider",
            "operator_issuer",
            "operator_subject",
            "idempotency_key",
            name="action_decision_provider_operator_idempotency_key",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(operator_issuer IS NULL) = (operator_subject IS NULL)", name="action_decision_operator_whole"
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"))
    verdict: Mapped[Verdict] = mapped_column(TextBackedStrEnumColumn(Verdict))
    provider: Mapped[str] = mapped_column(Text)
    # Set together for a human Decision, both NULL for a DecisionProvider's.
    operator_issuer: Mapped[str | None] = mapped_column(Text)
    operator_subject: Mapped[str | None] = mapped_column(Text)
    decision_note: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    reason_description: Mapped[str | None] = mapped_column(Text)
    # Only a policy-set Decision has one.
    policy_evidence: Mapped[PolicyEvidence | None] = mapped_column(PydanticColumn(PolicyEvidence))
    idempotency_key: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExecutionRow(Base):
    __tablename__ = "action_execution"
    __table_args__ = (
        UniqueConstraint("request_id"),
        Index(
            "ix_action_execution_lease_sweep",
            "lease_expires_at",
            postgresql_where=text("state IN ('dispatching', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"))
    state: Mapped[ExecutionState] = mapped_column(TextBackedStrEnumColumn(ExecutionState))
    result: Mapped[JsonValue | None] = mapped_column(JSONB)
    error: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The executor identity and unguessable bearer that authenticate every later
    # worker-originated call (heartbeat, completion) about this one Execution.
    executor_id: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only when a terminal outcome was learned after the Execution had already
    # become `execution_unknown`; never set on a first-pass finish.
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_source: Mapped[str | None] = mapped_column(Text)
    reconciled_by: Mapped[str | None] = mapped_column(Text)


@event.listens_for(ExecutionRow, "after_insert")
def _notify_dispatch(_mapper: Mapper[ExecutionRow], connection: Connection, row: ExecutionRow) -> None:
    # An Execution only ever enters `pending_dispatch` on insert; every replica's dispatcher wakes on commit.
    if row.state == ExecutionState.PENDING_DISPATCH:
        connection.execute(select(func.pg_notify(DISPATCH_CHANNEL, "")))


class ExecutorHeartbeatRow(Base):
    """Coarse-grained liveness of an executor identity, independent of any one Execution."""

    __tablename__ = "action_executor_heartbeat"

    executor_id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PushSubscriptionRow(Base):
    """One Web Push browser registration, scoped to the authenticated operator principal."""

    __tablename__ = "action_push_subscription"

    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    operator_issuer: Mapped[str] = mapped_column(Text)
    operator_subject: Mapped[str] = mapped_column(Text, index=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PushDeliveryRow(Base):
    """Last acknowledged notification state for one browser and Action."""

    __tablename__ = "action_push_delivery"
    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"), primary_key=True
    )
    endpoint: Mapped[str] = mapped_column(
        Text, ForeignKey("action_push_subscription.endpoint", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text)


class McpServerLinkageRow(Base):
    """One shared OAuth token family for a configured MCP server."""

    __tablename__ = "mcp_server_linkage"
    __table_args__ = (
        CheckConstraint(
            "(linked_by_issuer IS NULL) = (linked_by_subject IS NULL)", name="mcp_server_linkage_linked_by_whole"
        ),
    )

    server_id: Mapped[str] = mapped_column(Text, primary_key=True)
    server_url: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    token_state_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("mcp_oauth_token_state.id", ondelete="SET NULL"), nullable=True
    )
    token_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    linked_by_issuer: Mapped[str | None] = mapped_column(Text)
    linked_by_subject: Mapped[str | None] = mapped_column(Text)


class McpOAuthTokenStateRow(Base):
    """Normalized shared token state and cross-replica refresh claim."""

    __tablename__ = "mcp_oauth_token_state"
    __table_args__ = (
        UniqueConstraint("server_id"),
        CheckConstraint(
            "(refresh_failure_action IS NULL) = (refresh_failure_error IS NULL)",
            name="mcp_oauth_token_state_refresh_failure_whole",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    server_id: Mapped[str] = mapped_column(Text)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(Text)
    scope: Mapped[list[str]] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_claim_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    refresh_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_failure_count: Mapped[int] = mapped_column(Integer)
    refresh_failure_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_failure_latest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_failure_action: Mapped[str | None] = mapped_column(Text)
    refresh_failure_error: Mapped[str | None] = mapped_column(Text)
    refresh_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(McpServerLinkageRow, "after_insert")
@event.listens_for(McpServerLinkageRow, "after_update")
@event.listens_for(McpServerLinkageRow, "after_delete")
@event.listens_for(McpOAuthTokenStateRow, "after_insert")
@event.listens_for(McpOAuthTokenStateRow, "after_update")
@event.listens_for(McpOAuthTokenStateRow, "after_delete")
def _notify_mcp_linkage(_mapper: Mapper[object], connection: Connection, _row: object) -> None:
    # PostgreSQL delivers this only on commit, to stream readers on every Action Service replica.
    connection.execute(select(func.pg_notify(MCP_LINKAGE_CHANNEL, "")))


class McpLinkageFlowRow(Base):
    """Short-lived PKCE state; never expose the verifier or token to the browser."""

    __tablename__ = "mcp_linkage_flow"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    server_id: Mapped[str] = mapped_column(Text)
    state_hash: Mapped[str] = mapped_column(Text, unique=True)
    verifier: Mapped[str] = mapped_column(Text)
    operator_issuer: Mapped[str] = mapped_column(Text)
    operator_subject: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    authorization_endpoint: Mapped[str] = mapped_column(Text)
    token_endpoint: Mapped[str] = mapped_column(Text)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def make_engine(database_url: str) -> AsyncEngine:
    url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Staging OOM: 14 Action Service sessions were idle.
    return create_async_engine(url, pool_pre_ping=True, pool_size=4, max_overflow=2)


def make_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def verify_schema(engine: AsyncEngine) -> None:
    """Read every owned table without applying DDL; migrations are a separate deploy step."""
    async with engine.connect() as connection:
        for table in Base.metadata.tables.values():
            await connection.execute(select(table).limit(0))
