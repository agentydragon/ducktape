"""Postgres-owned canonical ActionRequest state and transactional lifecycle transitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    DecisionInput,
    DecisionView,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    ExecutionView,
    Principal,
    PrincipalRole,
    Verdict,
)

# SQLAlchemy loads asyncpg from the URL scheme; Gazelle cannot infer that runtime dependency.
# gazelle:include_dep @pypi//asyncpg

SessionMaker = async_sessionmaker[AsyncSession]


class Base(DeclarativeBase):
    pass


class ActionRequestRow(Base):
    __tablename__ = "action_request"
    __table_args__ = (UniqueConstraint("caller_principal", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    origin: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    correlation: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    caller_principal: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ActionEventRow(Base):
    __tablename__ = "action_event"

    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DecisionRow(Base):
    __tablename__ = "action_decision"
    __table_args__ = (UniqueConstraint("request_id"), UniqueConstraint("provider", "issuer", "idempotency_key"))

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"))
    verdict: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    issuer: Mapped[str] = mapped_column(Text)
    private_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExecutionRow(Base):
    __tablename__ = "action_execution"
    __table_args__ = (UniqueConstraint("request_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(Text)
    result: Mapped[JsonValue | None] = mapped_column(JSONB)
    error: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxRow(Base):
    """Pending-decision delivery reference; no credential-bearing arguments are copied here."""

    __tablename__ = "action_outbox"
    __table_args__ = (UniqueConstraint("request_id", "kind"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionNotFoundError(Exception):
    pass


class ActionConflictError(Exception):
    pass


class UnknownCapabilityError(Exception):
    pass


_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "setcookie",
        "token",
    }
)


def make_engine(database_url: str) -> AsyncEngine:
    url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def verify_schema(engine: AsyncEngine) -> None:
    """Read every owned table without applying DDL; migrations are a separate deploy step."""
    async with engine.connect() as connection:
        for table in Base.metadata.tables.values():
            await connection.execute(select(table).limit(0))


class ActionStore:
    def __init__(self, sessions: SessionMaker) -> None:
        self._sessions = sessions

    async def submit(
        self, body: ActionRequestInput, principal: Principal, *, supported_capabilities: frozenset[str]
    ) -> tuple[ActionRequestView, bool]:
        if body.capability not in supported_capabilities:
            raise UnknownCapabilityError(body.capability)
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(ActionRequestRow).where(
                    ActionRequestRow.caller_principal == principal.key,
                    ActionRequestRow.idempotency_key == body.idempotency_key,
                )
            )
            if existing is not None:
                if not _same_request(existing, body):
                    raise ActionConflictError("request idempotency key was already used for another envelope")
                return await self._view(session, existing, principal), False
            now = datetime.now(UTC)
            row = ActionRequestRow(
                idempotency_key=body.idempotency_key,
                capability=body.capability,
                arguments=body.arguments,
                origin=body.origin,
                correlation=body.correlation,
                caller_principal=principal.key,
                state=ActionState.DECISION_PENDING.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            _record_event(session, row, now)
            session.add(
                OutboxRow(
                    request_id=row.id,
                    kind="decision_pending",
                    payload={"request_id": str(row.id), "capability": row.capability},
                    created_at=now,
                )
            )
            return await self._view(session, row, principal), True

    async def list_requests(
        self, principal: Principal, *, states: Sequence[ActionState] = ()
    ) -> list[ActionRequestView]:
        query = select(ActionRequestRow).order_by(ActionRequestRow.created_at.desc())
        if principal.role is PrincipalRole.CALLER:
            query = query.where(ActionRequestRow.caller_principal == principal.key)
        if states:
            query = query.where(ActionRequestRow.state.in_([state.value for state in states]))
        async with self._sessions() as session:
            rows = list(await session.scalars(query))
            return [await self._view(session, row, principal) for row in rows]

    async def get(self, request_id: UUID, principal: Principal) -> ActionRequestView:
        async with self._sessions() as session:
            row = await session.get(ActionRequestRow, request_id)
            if row is None or not _may_read(row, principal):
                raise ActionNotFoundError(str(request_id))
            return await self._view(session, row, principal)

    async def events(self, request_id: UUID, principal: Principal) -> list[ActionEventView]:
        async with self._sessions() as session:
            row = await session.get(ActionRequestRow, request_id)
            if row is None or not _may_read(row, principal):
                raise ActionNotFoundError(str(request_id))
            events = list(
                await session.scalars(
                    select(ActionEventRow)
                    .where(ActionEventRow.request_id == request_id)
                    .order_by(ActionEventRow.sequence)
                )
            )
            return [ActionEventView(sequence=e.sequence, state=ActionState(e.state), at=e.at) for e in events]

    async def decide(
        self, request_id: UUID, body: DecisionInput, principal: Principal, *, provider: str
    ) -> tuple[ActionRequestView, bool]:
        if principal.role is not PrincipalRole.OPERATOR:
            raise ActionNotFoundError(str(request_id))
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            if row is None:
                raise ActionNotFoundError(str(request_id))
            prior_key = await session.scalar(
                select(DecisionRow).where(
                    DecisionRow.provider == provider,
                    DecisionRow.issuer == principal.key,
                    DecisionRow.idempotency_key == body.idempotency_key,
                )
            )
            if prior_key is not None:
                if prior_key.request_id != request_id or prior_key.verdict != body.verdict.value:
                    raise ActionConflictError("decision idempotency key was already used for another decision")
                return await self._view(session, row, principal), False
            if row.version != body.expected_version:
                raise ActionConflictError(
                    f"request changed: expected version {body.expected_version}, current version {row.version}"
                )
            if row.state != ActionState.DECISION_PENDING.value:
                raise ActionConflictError(f"request is already {row.state}")
            now = datetime.now(UTC)
            session.add(
                DecisionRow(
                    request_id=row.id,
                    verdict=body.verdict.value,
                    provider=provider,
                    issuer=principal.key,
                    private_reason=body.private_reason,
                    idempotency_key=body.idempotency_key,
                    decided_at=now,
                )
            )
            row.state = ActionState.ALLOWED.value if body.verdict is Verdict.ALLOW else ActionState.DENIED.value
            row.version += 1
            row.updated_at = now
            _record_event(session, row, now)
            should_dispatch = body.verdict is Verdict.ALLOW
            if should_dispatch:
                session.add(
                    ExecutionRow(request_id=row.id, state=ExecutionState.PENDING_DISPATCH.value, created_at=now)
                )
            await session.flush()
            return await self._view(session, row, principal), should_dispatch

    async def pending_dispatches(self) -> list[UUID]:
        async with self._sessions() as session:
            return list(
                await session.scalars(
                    select(ExecutionRow.request_id).where(ExecutionRow.state == ExecutionState.PENDING_DISPATCH.value)
                )
            )

    async def claim_execution(self, request_id: UUID) -> bool:
        """Atomically reserve the only execution before any adapter call can begin."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(str(request_id))
            if execution.state != ExecutionState.PENDING_DISPATCH.value:
                return False
            if row.state != ActionState.ALLOWED.value:
                raise ActionConflictError(f"cannot dispatch request in {row.state}")
            now = datetime.now(UTC)
            execution.state = ExecutionState.DISPATCHING.value
            row.state = ActionState.DISPATCHING.value
            row.version += 1
            row.updated_at = now
            _record_event(session, row, now)
            return True

    async def mark_running(self, request_id: UUID) -> ExecutionRequest:
        """Cross the no-replay boundary immediately before invoking the executor."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(str(request_id))
            if row.state != ActionState.DISPATCHING.value or execution.state != ExecutionState.DISPATCHING.value:
                raise ActionConflictError(f"cannot start execution in {row.state}/{execution.state}")
            now = datetime.now(UTC)
            execution.state = ExecutionState.RUNNING.value
            execution.started_at = now
            row.state = ActionState.RUNNING.value
            row.version += 1
            row.updated_at = now
            _record_event(session, row, now)
            return ExecutionRequest(
                request_id=row.id,
                capability=row.capability,
                arguments=row.arguments,
                origin=row.origin,
                correlation=row.correlation,
                caller_principal=row.caller_principal,
            )

    async def finish_execution(self, request_id: UUID, result: ExecutionResult) -> None:
        terminal = {
            ExecutionState.SUCCEEDED: ActionState.SUCCEEDED,
            ExecutionState.FAILED: ActionState.FAILED,
            ExecutionState.CANCELLED: ActionState.CANCELLED,
            ExecutionState.EXECUTION_UNKNOWN: ActionState.EXECUTION_UNKNOWN,
        }
        if result.state not in terminal:
            raise ValueError(f"executor returned non-terminal state {result.state}")
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(str(request_id))
            if execution.state not in {ExecutionState.DISPATCHING.value, ExecutionState.RUNNING.value}:
                raise ActionConflictError(f"cannot finish execution in {execution.state}")
            now = datetime.now(UTC)
            execution.state = result.state.value
            execution.result = result.result
            execution.error = result.error
            execution.completed_at = now
            row.state = terminal[result.state].value
            row.version += 1
            row.updated_at = now
            _record_event(session, row, now)

    async def recover_unknown(self) -> int:
        """Mark dispatches that crossed a process lifetime unknown; never replay them."""
        recovered = 0
        async with self._sessions.begin() as session:
            executions = list(
                await session.scalars(
                    select(ExecutionRow)
                    .where(ExecutionRow.state.in_([ExecutionState.DISPATCHING.value, ExecutionState.RUNNING.value]))
                    .with_for_update()
                )
            )
            for execution in executions:
                row = await session.scalar(
                    select(ActionRequestRow).where(ActionRequestRow.id == execution.request_id).with_for_update()
                )
                if row is None:
                    continue
                now = datetime.now(UTC)
                execution.state = ExecutionState.EXECUTION_UNKNOWN.value
                execution.error = {"kind": "process_restarted", "message": "dispatch outcome unknown; not replayed"}
                execution.completed_at = now
                row.state = ActionState.EXECUTION_UNKNOWN.value
                row.version += 1
                row.updated_at = now
                _record_event(session, row, now)
                recovered += 1
        return recovered

    async def _view(self, session: AsyncSession, row: ActionRequestRow, principal: Principal) -> ActionRequestView:
        decision = await session.scalar(select(DecisionRow).where(DecisionRow.request_id == row.id))
        execution = await session.scalar(select(ExecutionRow).where(ExecutionRow.request_id == row.id))
        operator = principal.role is PrincipalRole.OPERATOR
        return ActionRequestView(
            id=row.id,
            idempotency_key=row.idempotency_key,
            capability=row.capability,
            arguments=_redact(row.arguments),
            origin=_redact(row.origin),
            correlation=_redact(row.correlation),
            caller_principal=row.caller_principal if operator else None,
            state=ActionState(row.state),
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            decision=_decision_view(decision, operator=operator),
            execution=_execution_view(execution),
        )


def _same_request(row: ActionRequestRow, body: ActionRequestInput) -> bool:
    return (
        row.capability == body.capability
        and row.arguments == body.arguments
        and row.origin == body.origin
        and row.correlation == body.correlation
    )


def _may_read(row: ActionRequestRow, principal: Principal) -> bool:
    return principal.role is PrincipalRole.OPERATOR or row.caller_principal == principal.key


def _record_event(session: AsyncSession, row: ActionRequestRow, at: datetime) -> None:
    session.add(ActionEventRow(request_id=row.id, sequence=row.version, state=row.state, at=at))


def _decision_view(row: DecisionRow | None, *, operator: bool) -> DecisionView | None:
    if row is None:
        return None
    return DecisionView(
        id=row.id,
        verdict=Verdict(row.verdict),
        provider=row.provider,
        issuer=row.issuer,
        private_reason=row.private_reason if operator else None,
        private_reason_redacted=bool(row.private_reason) and not operator,
        idempotency_key=row.idempotency_key,
        decided_at=row.decided_at,
    )


def _execution_view(row: ExecutionRow | None) -> ExecutionView | None:
    if row is None:
        return None
    return ExecutionView(
        id=row.id,
        state=ExecutionState(row.state),
        result=_redact_value(row.result),
        error=_redact(row.error) if row.error is not None else None,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _redact(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: "[redacted]" if _secret_key(key) else _redact_value(item) for key, item in value.items()}


def _secret_key(key: str) -> bool:
    return key.lower().replace("_", "").replace("-", "") in _SECRET_KEYS


def _redact_value(value: JsonValue | None) -> JsonValue | None:
    if isinstance(value, dict):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def verify_metadata_for_connection(connection: Any) -> None:
    """Fail startup if this image's mappings cannot read the migrated schema."""
    for table in Base.metadata.tables.values():
        connection.execute(select(table).limit(0))
