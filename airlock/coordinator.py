"""Central coordinator for airlock: storage, pending decisions, and event bus.

Owns the SQLite-backed storage for action records and event log, the in-memory
pending decision futures, and a generic event bus that both the MCP server face
and the operator REST API subscribe to.

Schema:
  actions(session_key TEXT, action_seq INT, ...)  — composite PK
  event_log(session_key TEXT, entry_id INT, ...)  — composite PK, append-only

status is stored as a denormalised indexed column for fast pending queries;
it always matches action.state.status.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import Index, Integer, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from airlock.models import (
    Action,
    ActionKey,
    ActionReceivedDetail,
    ActionState,
    ActionStatus,
    LogEntry,
    LogEventDetail,
    OperatorDecision,
    PendingState,
    ToolCall,
    WithdrawnDetail,
    WithdrawnState,
)

logger = logging.getLogger(__name__)

_ACTION_STATE_TA: TypeAdapter[ActionState] = TypeAdapter(ActionState)
_LOG_DETAIL_TA: TypeAdapter[LogEventDetail] = TypeAdapter(LogEventDetail)


# ── Coordinator events ────────────────────────────────────────────────────────


@dataclass
class ActionCreatedEvent:
    action: Action


@dataclass
class ActionUpdatedEvent:
    action: Action


CoordinatorEvent = ActionCreatedEvent | ActionUpdatedEvent
CoordinatorListener = Callable[[CoordinatorEvent], Awaitable[None]]


# ── ORM models ────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


class _Base(DeclarativeBase):
    pass


class _ActionRow(_Base):
    __tablename__ = "actions"
    __table_args__ = (Index("idx_actions_status", "status"), Index("idx_actions_created", "created_at"))

    session_key: Mapped[str] = mapped_column(String, primary_key=True)
    action_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now, onupdate=_now)
    call_json: Mapped[str] = mapped_column(Text, nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)

    def to_action(self) -> Action:
        return Action(
            key=ActionKey(session_key=self.session_key, action_seq=self.action_seq),
            created_at=datetime.fromisoformat(self.created_at),
            updated_at=datetime.fromisoformat(self.updated_at),
            call=ToolCall.model_validate_json(self.call_json),
            justification=self.justification,
            state=_ACTION_STATE_TA.validate_json(self.state_json),
            client_id=self.client_id,
            subject=self.subject,
        )


class _LogEntryRow(_Base):
    __tablename__ = "event_log"
    __table_args__ = (Index("idx_log_session_action", "session_key", "action_seq"),)

    session_key: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False, default=_now)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False)

    def to_log_entry(self) -> LogEntry:
        return LogEntry(
            entry_id=self.entry_id,
            session_key=self.session_key,
            action_seq=self.action_seq,
            detail=_LOG_DETAIL_TA.validate_json(self.detail_json),
            timestamp=datetime.fromisoformat(self.timestamp),
        )


# ── Coordinator ───────────────────────────────────────────────────────────────


class ActionCoordinator:
    """Central coordinator: storage, pending decisions, and event bus.

    Both the MCP server face and the operator REST API operate on the same
    coordinator instance. State mutations emit events that listeners translate
    into their own notification mechanisms (MCP resource-updated, SSE).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._pending: dict[ActionKey, asyncio.Future[OperatorDecision]] = {}
        self._listeners: list[CoordinatorListener] = []

    async def initialize(self) -> None:
        """Open the database, create schema if needed."""
        if self._db_path is None:
            raise RuntimeError("cannot initialize coordinator without a db_path")
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._db_path}")
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        """Dispose of the underlying engine, releasing all connections."""
        if self._engine is not None:
            await self._engine.dispose()

    @property
    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("coordinator not initialised — call initialize() first")
        return self._session_factory

    # ── Event bus ─────────────────────────────────────────────────────────────

    def add_listener(self, listener: CoordinatorListener) -> None:
        self._listeners.append(listener)

    async def _emit(self, event: CoordinatorEvent) -> None:
        for listener in self._listeners:
            await listener(event)

    # ── Pending decisions ──────────────────────────────────────────────────────

    def register_pending(self, key: ActionKey) -> asyncio.Future[OperatorDecision]:
        """Create and register a future for a pending human decision."""
        fut: asyncio.Future[OperatorDecision] = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        return fut

    def remove_pending(self, key: ActionKey) -> asyncio.Future[OperatorDecision] | None:
        """Remove and return the pending future, if any."""
        return self._pending.pop(key, None)

    async def decide(self, key: ActionKey, decision: OperatorDecision) -> None:
        """Resolve a pending decision. Raises ValueError if not decidable."""
        action = await self.get_action(key)
        if action is None:
            raise ValueError(f"Action not found: {key}")
        if not isinstance(action.state, PendingState):
            raise ValueError(f"Action {key} is not pending ({action.state.status=})")
        fut = self._pending.get(key)
        if fut is None or fut.done():
            raise ValueError(f"Action {key} is not awaiting a human decision")
        fut.set_result(decision)

    # ── Action CRUD ──────────────────────────────────────────────────────────

    async def create_action(
        self, *, session_key: str, call: ToolCall, justification: str, client_id: str | None, subject: str | None
    ) -> Action:
        """Insert a new pending action, append ActionReceived log, and emit event."""
        state = PendingState()
        async with self._sessions() as session:
            result = await session.execute(
                select(func.coalesce(func.max(_ActionRow.action_seq), 0)).where(_ActionRow.session_key == session_key)
            )
            action_seq = result.scalar_one() + 1
            row = _ActionRow(
                session_key=session_key,
                action_seq=action_seq,
                call_json=call.model_dump_json(),
                justification=justification,
                state_json=state.model_dump_json(),
                status=state.status,
                client_id=client_id,
                subject=subject,
            )
            session.add(row)
            next_id = (await self._get_log_hwm(session, session_key)) + 1
            await self._add_log_row(
                session, session_key=session_key, action_seq=action_seq, next_id=next_id, detail=ActionReceivedDetail()
            )
            await session.commit()
        action = row.to_action()
        logger.debug("created action %s tool=%s", action.key, call.tool_name)
        await self._emit(ActionCreatedEvent(action=action))
        return action

    async def get_action(self, key: ActionKey) -> Action | None:
        """Fetch a single action by compound key."""
        async with self._sessions() as session:
            row = await session.get(_ActionRow, (key.session_key, key.action_seq))
        if row is None:
            return None
        return row.to_action()

    async def update_and_log(self, key: ActionKey, new_state: ActionState, detail: LogEventDetail) -> Action:
        """Update action state, append log entry atomically, and emit event."""
        async with self._sessions() as session:
            action_row = await session.get(_ActionRow, (key.session_key, key.action_seq))
            if action_row is None:
                raise ValueError(f"Action not found: {key}")
            action_row.state_json = _ACTION_STATE_TA.dump_json(new_state).decode()
            action_row.status = new_state.status
            next_id = (await self._get_log_hwm(session, key.session_key)) + 1
            await self._add_log_row(
                session, session_key=key.session_key, action_seq=key.action_seq, next_id=next_id, detail=detail
            )
            await session.commit()
            await session.refresh(action_row)
        action = action_row.to_action()
        await self._emit(ActionUpdatedEvent(action=action))
        return action

    async def withdraw(self, key: ActionKey) -> Action:
        """Withdraw a pending action. Validates, updates state, cancels future, emits event."""
        action = await self.get_action(key)
        if action is None:
            raise ValueError(f"Action not found: {key}")
        if not isinstance(action.state, PendingState):
            raise ValueError(f"Action {key} is not pending ({action.state.status=})")
        result = await self.update_and_log(key, WithdrawnState(), WithdrawnDetail())
        fut = self.remove_pending(key)
        if fut is not None and not fut.done():
            fut.cancel()
        return result

    async def list_actions(
        self, status: ActionStatus | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[Action]:
        """List actions, optionally filtered by status, newest first."""
        async with self._sessions() as session:
            stmt = select(_ActionRow).order_by(_ActionRow.created_at.desc()).limit(limit).offset(offset)
            if status is not None:
                stmt = stmt.where(_ActionRow.status == status)
            return [r.to_action() for r in (await session.execute(stmt)).scalars().all()]

    # ── Event log ────────────────────────────────────────────────────────────

    @staticmethod
    async def _get_log_hwm(session: AsyncSession, session_key: str) -> int:
        result = await session.execute(
            select(func.coalesce(func.max(_LogEntryRow.entry_id), 0)).where(_LogEntryRow.session_key == session_key)
        )
        return result.scalar_one()

    @staticmethod
    async def _add_log_row(
        session: AsyncSession, *, session_key: str, action_seq: int, next_id: int, detail: LogEventDetail
    ) -> _LogEntryRow:
        row = _LogEntryRow(
            session_key=session_key,
            entry_id=next_id,
            action_seq=action_seq,
            kind=detail.kind,
            detail_json=_LOG_DETAIL_TA.dump_json(detail).decode(),
        )
        session.add(row)
        return row

    async def get_log_hwm(self, session_key: str) -> int:
        """Return the highest entry_id for a session, or 0 if no entries."""
        async with self._sessions() as session:
            return await self._get_log_hwm(session, session_key)

    async def get_log_entry(self, session_key: str, entry_id: int) -> LogEntry | None:
        """Fetch a specific log entry."""
        async with self._sessions() as session:
            row = await session.get(_LogEntryRow, (session_key, entry_id))
        if row is None:
            return None
        return row.to_log_entry()

    async def get_log_entries_since(self, session_key: str, after_entry_id: int) -> list[LogEntry]:
        """Fetch all log entries after a given entry_id for catch-up."""
        async with self._sessions() as session:
            result = await session.execute(
                select(_LogEntryRow)
                .where(_LogEntryRow.session_key == session_key, _LogEntryRow.entry_id > after_entry_id)
                .order_by(_LogEntryRow.entry_id.asc())
            )
            rows = result.scalars().all()
        return [r.to_log_entry() for r in rows]
