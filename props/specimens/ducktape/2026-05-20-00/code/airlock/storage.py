"""PostgreSQL-backed storage for action records and append-only event log.

Uses SQLAlchemy async ORM with the asyncpg driver and Alembic for schema migrations.

Schema:
  actions(session_key UUID, action_seq INT, ...)         — composite PK
  action_seq_counters(session_key UUID, next_seq INT)    — per-session sequence
  event_log(session_key UUID, entry_id INT, ...)         — composite PK, append-only

status is stored as a denormalised indexed column for fast pending queries;
it always matches action.state.status.

Migrations are applied automatically on ActionStorage.initialize().
Add new migrations in airlock/migrations/versions/.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from pydantic import TypeAdapter
from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from airlock.models import (
    Action,
    ActionKey,
    ActionState,
    ActionStatus,
    LogEntry,
    LogEventDetail,
    PendingState,
    ToolCall,
)

logger = logging.getLogger(__name__)

_ACTION_STATE_TA: TypeAdapter[ActionState] = TypeAdapter(ActionState)
_LOG_DETAIL_TA: TypeAdapter[LogEventDetail] = TypeAdapter(LogEventDetail)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _run_alembic_migrations(conn: Any) -> None:
    """Run all pending Alembic migrations. Called synchronously via run_sync."""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    alembic_command.upgrade(cfg, "head")


class _Base(DeclarativeBase):
    pass


class _ActionRow(_Base):
    __tablename__ = "actions"
    __table_args__ = (Index("idx_actions_status", "status"), Index("idx_actions_created", "created_at"))

    session_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True, native_uuid=True), primary_key=True)
    action_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    call_json: Mapped[str] = mapped_column(Text, nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    def to_action(self) -> Action:
        return Action(
            key=ActionKey(session_key=self.session_key, action_seq=self.action_seq),
            created_at=self.created_at,
            updated_at=self.updated_at,
            call=ToolCall.model_validate_json(self.call_json),
            justification=self.justification,
            state=_ACTION_STATE_TA.validate_json(self.state_json),
            client_id=self.client_id,
        )


class _ActionSeqCounterRow(_Base):
    __tablename__ = "action_seq_counters"

    session_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True, native_uuid=True), primary_key=True)
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False)


class _LogEntryRow(_Base):
    __tablename__ = "event_log"
    __table_args__ = (Index("idx_log_session_action", "session_key", "action_seq"),)

    session_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True, native_uuid=True), primary_key=True)
    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False)

    def to_log_entry(self) -> LogEntry:
        return LogEntry(
            entry_id=self.entry_id,
            session_key=self.session_key,
            action_seq=self.action_seq,
            detail=_LOG_DETAIL_TA.validate_json(self.detail_json),
            timestamp=self.timestamp,
        )


class ActionStorage:
    """Async PostgreSQL storage for action records and event log."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine) -> None:
        self._session_factory = session_factory
        self._engine = engine

    @classmethod
    async def initialize(cls, db_url: str) -> ActionStorage:
        """Open the database, run Alembic migrations to head, and return a ready storage."""
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.run_sync(_run_alembic_migrations)
        return cls(async_sessionmaker(engine, expire_on_commit=False), engine)

    async def close(self) -> None:
        """Dispose of the underlying engine, releasing all connections."""
        await self._engine.dispose()

    # ── Action CRUD ──────────────────────────────────────────────────────────

    async def create_action(
        self, *, session_key: UUID, call: ToolCall, justification: str, client_id: str | None = None
    ) -> Action:
        """Insert a new pending action, atomically assigning the next action_seq.

        Uses a per-session counter table with PostgreSQL atomic upsert to safely
        assign monotonically increasing action_seq values even under concurrent writers.
        """
        state = PendingState()
        async with self._session_factory() as session:
            # Atomically increment (or initialise) the per-session sequence counter.
            stmt = (
                pg_insert(_ActionSeqCounterRow)
                .values(session_key=session_key, next_seq=1)
                .on_conflict_do_update(
                    index_elements=["session_key"], set_={"next_seq": _ActionSeqCounterRow.next_seq + 1}
                )
                .returning(_ActionSeqCounterRow.next_seq)
            )
            action_seq = (await session.execute(stmt)).scalar_one()
            row = _ActionRow(
                session_key=session_key,
                action_seq=action_seq,
                call_json=call.model_dump_json(),
                justification=justification,
                state_json=state.model_dump_json(),
                status=state.status,
                client_id=client_id,
            )
            session.add(row)
            await session.commit()
        logger.debug("created action %s/%d tool=%s", session_key, action_seq, call.tool_name)
        return row.to_action()

    async def get_action(self, key: ActionKey) -> Action | None:
        """Fetch a single action by compound key."""
        async with self._session_factory() as session:
            row = await session.get(_ActionRow, (key.session_key, key.action_seq))
        if row is None:
            return None
        return row.to_action()

    async def update_state(self, key: ActionKey, new_state: ActionState) -> Action | None:
        """Replace the state of an existing action; returns updated action or None."""
        async with self._session_factory() as session:
            row = await session.get(_ActionRow, (key.session_key, key.action_seq))
            if row is None:
                return None
            row.state_json = _ACTION_STATE_TA.dump_json(new_state).decode()
            row.status = new_state.status
            await session.commit()
            await session.refresh(row)
        return row.to_action()

    async def update_state_and_log(
        self, key: ActionKey, new_state: ActionState, detail: LogEventDetail
    ) -> tuple[Action, LogEntry]:
        """Update action state and append a log entry in one transaction."""
        async with self._session_factory() as session:
            action_row = await session.get(_ActionRow, (key.session_key, key.action_seq))
            if action_row is None:
                raise ValueError(f"Action not found: {key.session_key}/{key.action_seq}")
            action_row.state_json = _ACTION_STATE_TA.dump_json(new_state).decode()
            action_row.status = new_state.status

            next_id = (await self._get_log_hwm(session, key.session_key)) + 1
            log_row = await self._add_log_row(
                session, session_key=key.session_key, action_seq=key.action_seq, next_id=next_id, detail=detail
            )
            await session.commit()
            await session.refresh(action_row)
        return action_row.to_action(), log_row.to_log_entry()

    async def list_actions(
        self, status: ActionStatus | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[Action]:
        """List actions, optionally filtered by status, newest first."""
        async with self._session_factory() as session:
            stmt = select(_ActionRow).order_by(_ActionRow.created_at.desc()).limit(limit).offset(offset)
            if status is not None:
                stmt = stmt.where(_ActionRow.status == status)
            return [r.to_action() for r in (await session.execute(stmt)).scalars().all()]

    # ── Event log ────────────────────────────────────────────────────────────

    @staticmethod
    async def _get_log_hwm(session: AsyncSession, session_key: UUID) -> int:
        result = await session.execute(
            select(func.coalesce(func.max(_LogEntryRow.entry_id), 0)).where(_LogEntryRow.session_key == session_key)
        )
        return result.scalar_one()

    @staticmethod
    async def _add_log_row(
        session: AsyncSession, *, session_key: UUID, action_seq: int, next_id: int, detail: LogEventDetail
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

    async def append_log_entry(self, *, session_key: UUID, action_seq: int, detail: LogEventDetail) -> LogEntry:
        """Append an event to the log; assigns the next entry_id for the session."""
        async with self._session_factory() as session:
            next_id = (await self._get_log_hwm(session, session_key)) + 1
            row = await self._add_log_row(
                session, session_key=session_key, action_seq=action_seq, next_id=next_id, detail=detail
            )
            await session.commit()
        return row.to_log_entry()

    async def get_log_hwm(self, session_key: UUID) -> int:
        """Return the highest entry_id for a session, or 0 if no entries."""
        async with self._session_factory() as session:
            return await self._get_log_hwm(session, session_key)

    async def get_log_entry(self, session_key: UUID, entry_id: int) -> LogEntry | None:
        """Fetch a specific log entry."""
        async with self._session_factory() as session:
            row = await session.get(_LogEntryRow, (session_key, entry_id))
        if row is None:
            return None
        return row.to_log_entry()

    async def get_log_entries_since(self, session_key: UUID, after_entry_id: int) -> list[LogEntry]:
        """Fetch all log entries after a given entry_id for catch-up."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(_LogEntryRow)
                .where(_LogEntryRow.session_key == session_key, _LogEntryRow.entry_id > after_entry_id)
                .order_by(_LogEntryRow.entry_id.asc())
            )
            rows = result.scalars().all()
        return [r.to_log_entry() for r in rows]
