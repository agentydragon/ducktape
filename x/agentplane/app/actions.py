"""Durable ActionRequest coordination in the integration-app process.

The hub owns immutable requests, final Decisions, and at most one Execution.  Decision providers
and executors are separate interfaces even though v0 colocates a human provider and an explicit
fixture executor here.  There are no retries: once dispatch may have started, uncertainty is
recorded as ``execution_unknown`` rather than replayed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from x.agentplane.app.identity import CallerIdentity, CallerKind
from x.agentplane.app.trajectory import Thread


class ActionBase(DeclarativeBase):
    pass


class ActionState(StrEnum):
    DECISION_PENDING = "decision_pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXECUTION_UNKNOWN = "execution_unknown"


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


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


class ActionRequestRow(ActionBase):
    __tablename__ = "action_request"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    capability: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    # The originating Thread lives in TrajectoryStore's metadata; submit() verifies it in the same
    # database before this row is inserted rather than coupling the two modules' SQLAlchemy metadata.
    origin_thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    caller_kind: Mapped[str] = mapped_column(Text)
    caller_principal: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ActionEventRow(ActionBase):
    """Append-only state evidence; sequence is the request projection version."""

    __tablename__ = "action_event"

    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    state: Mapped[str] = mapped_column(Text)


class DecisionRow(ActionBase):
    __tablename__ = "action_decision"
    __table_args__ = (UniqueConstraint("request_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"))
    verdict: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    issuer: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ExecutionRow(ActionBase):
    __tablename__ = "action_execution"
    __table_args__ = (UniqueConstraint("request_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("action_request.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(Text)
    result: Mapped[JsonValue | None] = mapped_column(JSONB)
    error: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=240)
    arguments: dict[str, JsonValue]
    origin_thread_id: UUID


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


class DecisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    verdict: Verdict
    provider: str
    issuer: str
    reason: str | None
    idempotency_key: str
    decided_at: datetime


class ExecutionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    state: ActionState
    result: JsonValue | None
    error: dict[str, JsonValue] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ActionRequestView(BaseModel):
    """The authorized, redacted projection returned to callers and operators."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    capability: str
    arguments: dict[str, JsonValue]
    origin_thread_id: UUID
    caller_kind: CallerKind
    caller_principal: str
    state: ActionState
    version: int
    created_at: datetime
    updated_at: datetime
    decision: DecisionView | None
    execution: ExecutionView | None


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    capability: str
    arguments: dict[str, JsonValue]
    origin_thread_id: UUID
    caller_principal: str


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ActionState
    result: JsonValue | None = None
    error: dict[str, JsonValue] | None = None


class Executor(Protocol):
    @property
    def capabilities(self) -> frozenset[str]: ...

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class ExecutionOutcomeUnknownError(Exception):
    """Dispatch may have reached its target, so retrying would be unsafe."""


class EchoExecutor:
    """Explicit v0 fixture adapter: exercises dispatch without claiming an MCP integration exists."""

    CAPABILITY = "agentplane:v0.echo"

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({self.CAPABILITY})

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(state=ActionState.SUCCEEDED, result={"echo": request.arguments})


class ActionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    state: ActionState
    version: int


class ActionEventSink(Protocol):
    """Future notification adapters consume this seam and send decisions back through the provider."""

    async def publish(self, event: ActionEvent) -> None: ...


class NullActionEventSink:
    async def publish(self, event: ActionEvent) -> None:
        del event


class ActionNotFoundError(Exception):
    def __init__(self, request_id: UUID) -> None:
        super().__init__(f"no action request {request_id}")


class UnknownCapabilityError(Exception):
    def __init__(self, capability: str) -> None:
        super().__init__(f"unsupported action capability {capability!r}")


class UnknownOriginThreadError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no origin thread {thread_id}")


class ActionConflictError(Exception):
    pass


class ActionHub:
    def __init__(self, engine: AsyncEngine, executor: Executor, *, events: ActionEventSink | None = None) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._executor = executor
        self._events = events or NullActionEventSink()
        self._tasks: set[asyncio.Task[None]] = set()

    async def ensure_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(ActionBase.metadata.create_all)

    async def recover_uncertain_executions(self) -> int:
        """After process loss, never replay work whose dispatch may already have reached a target."""
        recovered: list[ActionEvent] = []
        async with self._sessions.begin() as session:
            rows = list(
                await session.scalars(
                    select(ActionRequestRow)
                    .join(ExecutionRow, ExecutionRow.request_id == ActionRequestRow.id)
                    .where(ExecutionRow.state.in_([ActionState.DISPATCHING.value, ActionState.RUNNING.value]))
                    .with_for_update()
                )
            )
            now = datetime.now(UTC)
            for row in rows:
                execution = await session.scalar(select(ExecutionRow).where(ExecutionRow.request_id == row.id))
                if execution is None:
                    continue
                row.state = ActionState.EXECUTION_UNKNOWN.value
                row.version += 1
                row.updated_at = now
                execution.state = ActionState.EXECUTION_UNKNOWN.value
                execution.error = {"kind": "process_restarted", "message": "dispatch outcome is unknown; not replayed"}
                execution.completed_at = now
                _record_event(session, row)
                recovered.append(_event(row))
        for event in recovered:
            await self._events.publish(event)
        return len(recovered)

    async def close(self) -> None:
        if not self._tasks:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def submit(self, body: NewActionRequest, caller: CallerIdentity) -> ActionRequestView:
        if body.capability not in self._executor.capabilities:
            raise UnknownCapabilityError(body.capability)
        async with self._sessions.begin() as session:
            if await session.get(Thread, body.origin_thread_id) is None:
                raise UnknownOriginThreadError(body.origin_thread_id)
            row = ActionRequestRow(
                capability=body.capability,
                arguments=body.arguments,
                origin_thread_id=body.origin_thread_id,
                caller_kind=caller.kind.value,
                caller_principal=caller.principal,
                state=ActionState.DECISION_PENDING.value,
                version=1,
            )
            session.add(row)
            await session.flush()
            _record_event(session, row)
            view = await self._view(session, row)
        await self._events.publish(_event(row))
        return view

    async def list_requests(
        self, caller: CallerIdentity, *, states: Sequence[ActionState] = ()
    ) -> list[ActionRequestView]:
        query = select(ActionRequestRow).order_by(ActionRequestRow.created_at.desc())
        if caller.kind is not CallerKind.OPERATOR:
            query = query.where(
                ActionRequestRow.caller_kind == caller.kind.value, ActionRequestRow.caller_principal == caller.principal
            )
        if states:
            query = query.where(ActionRequestRow.state.in_([state.value for state in states]))
        async with self._sessions() as session:
            rows = list(await session.scalars(query))
            return [await self._view(session, row) for row in rows]

    async def get(self, request_id: UUID, caller: CallerIdentity) -> ActionRequestView:
        async with self._sessions() as session:
            row = await session.get(ActionRequestRow, request_id)
            if row is None or not _may_read(row, caller):
                raise ActionNotFoundError(request_id)
            return await self._view(session, row)

    async def history(self, request_id: UUID, caller: CallerIdentity) -> list[ActionState]:
        """Append-only lifecycle evidence, primarily for tests and future delivery adapters."""
        async with self._sessions() as session:
            row = await session.get(ActionRequestRow, request_id)
            if row is None or not _may_read(row, caller):
                raise ActionNotFoundError(request_id)
            states = await session.scalars(
                select(ActionEventRow.state)
                .where(ActionEventRow.request_id == request_id)
                .order_by(ActionEventRow.sequence)
            )
            return [ActionState(state) for state in states]

    async def decide(self, request_id: UUID, body: DecisionInput, *, issuer: str, provider: str) -> ActionRequestView:
        should_dispatch = False
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            if row is None:
                raise ActionNotFoundError(request_id)
            prior = await session.scalar(
                select(DecisionRow).where(
                    DecisionRow.request_id == request_id,
                    DecisionRow.provider == provider,
                    DecisionRow.issuer == issuer,
                    DecisionRow.idempotency_key == body.idempotency_key,
                )
            )
            if prior is not None:
                if prior.verdict != body.verdict.value:
                    raise ActionConflictError("decision idempotency key was already used for another verdict")
                return await self._view(session, row)
            if row.version != body.expected_version:
                raise ActionConflictError(
                    f"action request changed: expected version {body.expected_version}, current version {row.version}"
                )
            if row.state != ActionState.DECISION_PENDING.value:
                raise ActionConflictError(f"action request is already {row.state}")

            decision = DecisionRow(
                request_id=row.id,
                verdict=body.verdict.value,
                provider=provider,
                issuer=issuer,
                reason=body.reason,
                idempotency_key=body.idempotency_key,
            )
            session.add(decision)
            row.state = ActionState.ALLOWED.value if body.verdict is Verdict.ALLOW else ActionState.DENIED.value
            row.version += 1
            row.updated_at = datetime.now(UTC)
            if body.verdict is Verdict.ALLOW:
                session.add(ExecutionRow(request_id=row.id, state=ActionState.DISPATCHING.value))
                should_dispatch = True
            _record_event(session, row)
            await session.flush()
            view = await self._view(session, row)
            event = _event(row)
        await self._events.publish(event)
        if should_dispatch:
            self._schedule(request_id)
        return view

    def _schedule(self, request_id: UUID) -> None:
        task = asyncio.create_task(self._run_execution(request_id), name=f"action-execution-{request_id}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _run_execution(self, request_id: UUID) -> None:
        try:
            await self._mark_dispatching(request_id)
            await asyncio.sleep(0)
            request = await self._mark_running(request_id)
            result = await self._executor.execute(request)
            if result.state not in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELLED}:
                raise ValueError(f"executor returned non-terminal state {result.state}")
        except ExecutionOutcomeUnknownError as error:
            result = ExecutionResult(
                state=ActionState.EXECUTION_UNKNOWN, error={"kind": "execution_outcome_unknown", "message": str(error)}
            )
        except asyncio.CancelledError:
            await self._finish_execution(
                request_id,
                ExecutionResult(
                    state=ActionState.EXECUTION_UNKNOWN,
                    error={"kind": "coordinator_stopped", "message": "dispatch outcome is unknown; not replayed"},
                ),
            )
            raise
        except Exception as error:
            result = ExecutionResult(
                state=ActionState.FAILED, error={"kind": type(error).__name__, "message": str(error)}
            )
        await self._finish_execution(request_id, result)

    async def _mark_dispatching(self, request_id: UUID) -> None:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            if row is None:
                raise ActionNotFoundError(request_id)
            if row.state != ActionState.ALLOWED.value:
                raise ActionConflictError(f"cannot begin dispatch in state {row.state}")
            row.state = ActionState.DISPATCHING.value
            row.version += 1
            row.updated_at = datetime.now(UTC)
            _record_event(session, row)
            event = _event(row)
        await self._events.publish(event)

    async def _mark_running(self, request_id: UUID) -> ExecutionRequest:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(request_id)
            if row.state != ActionState.DISPATCHING.value or execution.state != ActionState.DISPATCHING.value:
                raise ActionConflictError(f"cannot dispatch action request in state {row.state}")
            now = datetime.now(UTC)
            row.state = ActionState.RUNNING.value
            row.version += 1
            row.updated_at = now
            execution.state = ActionState.RUNNING.value
            execution.started_at = now
            _record_event(session, row)
            request = ExecutionRequest(
                request_id=row.id,
                capability=row.capability,
                arguments=row.arguments,
                origin_thread_id=row.origin_thread_id,
                caller_principal=row.caller_principal,
            )
            event = _event(row)
        await self._events.publish(event)
        return request

    async def _finish_execution(self, request_id: UUID, result: ExecutionResult) -> None:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(request_id)
            if row.state not in {ActionState.ALLOWED.value, ActionState.DISPATCHING.value, ActionState.RUNNING.value}:
                raise ActionConflictError(f"cannot finish action request in state {row.state}")
            now = datetime.now(UTC)
            row.state = result.state.value
            row.version += 1
            row.updated_at = now
            execution.state = result.state.value
            execution.result = result.result
            execution.error = result.error
            execution.completed_at = now
            _record_event(session, row)
            event = _event(row)
        await self._events.publish(event)

    async def _view(self, session: AsyncSession, row: ActionRequestRow) -> ActionRequestView:
        decision = await session.scalar(select(DecisionRow).where(DecisionRow.request_id == row.id))
        execution = await session.scalar(select(ExecutionRow).where(ExecutionRow.request_id == row.id))
        return ActionRequestView(
            id=row.id,
            capability=row.capability,
            arguments=_redact(row.arguments),
            origin_thread_id=row.origin_thread_id,
            caller_kind=CallerKind(row.caller_kind),
            caller_principal=row.caller_principal,
            state=ActionState(row.state),
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            decision=_decision_view(decision),
            execution=_execution_view(execution),
        )


class HumanDecisionProvider:
    """The existing authenticated operator session is the v0 human Decision Authority."""

    NAME = "human_operator"

    def __init__(self, hub: ActionHub) -> None:
        self._hub = hub

    async def decide(self, request_id: UUID, body: DecisionInput, caller: CallerIdentity) -> ActionRequestView:
        if caller.kind is not CallerKind.OPERATOR:
            raise ActionNotFoundError(request_id)
        return await self._hub.decide(request_id, body, issuer=caller.principal, provider=self.NAME)


def _may_read(row: ActionRequestRow, caller: CallerIdentity) -> bool:
    return caller.kind is CallerKind.OPERATOR or (
        row.caller_kind == caller.kind.value and row.caller_principal == caller.principal
    )


def _event(row: ActionRequestRow) -> ActionEvent:
    return ActionEvent(request_id=row.id, state=ActionState(row.state), version=row.version)


def _record_event(session: AsyncSession, row: ActionRequestRow) -> None:
    session.add(ActionEventRow(request_id=row.id, sequence=row.version, state=row.state))


def _decision_view(row: DecisionRow | None) -> DecisionView | None:
    if row is None:
        return None
    return DecisionView(
        id=row.id,
        verdict=Verdict(row.verdict),
        provider=row.provider,
        issuer=row.issuer,
        reason=row.reason,
        idempotency_key=row.idempotency_key,
        decided_at=row.decided_at,
    )


def _execution_view(row: ExecutionRow | None) -> ExecutionView | None:
    if row is None:
        return None
    return ExecutionView(
        id=row.id,
        state=ActionState(row.state),
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
