"""One session's command state and exact replayable Events, committed together."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner.observation import Observation, observation_event

# gazelle:include_dep @pypi//aiosqlite
# gazelle:include_dep @pypi//protobuf


class Base(DeclarativeBase):
    pass


class EventEntry(Base):
    __tablename__ = "event_entry"

    cursor: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class Command(Base):
    __tablename__ = "command"

    command_id: Mapped[str] = mapped_column(primary_key=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    admitted_cursor: Mapped[int] = mapped_column(ForeignKey("event_entry.cursor"))
    dispatch_planned: Mapped[bool] = mapped_column(default=False)
    native_correlation: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    terminal_cursor: Mapped[int | None] = mapped_column(ForeignKey("event_entry.cursor"))

    __table_args__ = (Index("pending_command_admission", "terminal_cursor", "admitted_cursor"),)


class Checkpoint(Base):
    __tablename__ = "checkpoint"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str]
    through_cursor: Mapped[int]
    harness_running: Mapped[bool]
    active_turn_id: Mapped[str]


class DebugCheckpoint(Base):
    __tablename__ = "debug_checkpoint"

    name: Mapped[str] = mapped_column(primary_key=True)
    command_id: Mapped[str] = mapped_column(primary_key=True)


class ScratchBase(DeclarativeBase):
    pass


class AdapterItem(ScratchBase):
    __tablename__ = "adapter_item"
    __table_args__ = ({"prefixes": ["TEMPORARY"]},)

    adapter_id: Mapped[str] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(primary_key=True)


class AdapterMessage(ScratchBase):
    __tablename__ = "adapter_message"
    __table_args__ = ({"prefixes": ["TEMPORARY"]},)

    adapter_id: Mapped[str] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(primary_key=True)
    next_block: Mapped[int]


@dataclass(frozen=True)
class RecoveryState:
    through_cursor: int
    harness_running: bool
    active_turn_id: str


def _apply_checkpoint(checkpoint: Checkpoint, entry: event_log_pb2.EventEntry) -> None:
    checkpoint.through_cursor = entry.cursor
    event = entry.event
    match event.WhichOneof("observation"):
        case "harness_started":
            checkpoint.harness_running = True
        case "harness_lost" | "harness_exited":
            checkpoint.harness_running = False
        case "turn_started":
            checkpoint.active_turn_id = event.turn_started.turn_id
        case "turn_completed":
            checkpoint.active_turn_id = ""


class CommandConflictError(ValueError):
    """One command id was reused with different requested work."""


class JournalStorageError(OSError):
    """Publication stopped; reopen the database before serving this source again."""


class Journal:
    def __init__(self, connection: AsyncConnection, engine: AsyncEngine, source_id: str, state: RecoveryState) -> None:
        self._connection = connection
        self._engine = engine
        self.source_id = source_id
        self._state = state
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._failure: BaseException | None = None

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path, source_id: str) -> AsyncIterator[Journal]:
        # A reader uses a separate connection so it cannot see an uncommitted append.
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}", connect_args={"isolation_level": None}, pool_size=2, max_overflow=0
        )
        try:
            async with engine.connect() as connection:
                # EXTRA includes the rollback-journal unlink directory fence.
                await connection.execute(text("PRAGMA journal_mode=DELETE"))
                await connection.execute(text("PRAGMA synchronous=EXTRA"))
                await connection.execute(text("PRAGMA foreign_keys=ON"))
                # Historical adapter lookup keys spill to a connection-local scratch file.
                # Its bounded page cache is separate from durable journal storage.
                await connection.execute(text("PRAGMA temp_store=FILE"))
                await connection.execute(text("PRAGMA temp.cache_size=-2048"))
                await connection.commit()
                async with connection.begin():
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.run_sync(ScratchBase.metadata.create_all)
                async with AsyncSession(connection, expire_on_commit=False) as session, session.begin():
                    await session.execute(text("BEGIN"))
                    last = await session.scalar(select(EventEntry).order_by(EventEntry.cursor.desc()).limit(1))
                    checkpoint = await session.get(Checkpoint, 1)
                    if checkpoint is None:
                        if last is not None:
                            raise ValueError("journal has no recovery checkpoint")
                        checkpoint = Checkpoint(
                            id=1, source_id=source_id, through_cursor=0, harness_running=False, active_turn_id=""
                        )
                        session.add(checkpoint)
                    if checkpoint.source_id != source_id:
                        raise ValueError("journal recovery checkpoint source does not match")
                    if checkpoint.through_cursor != (last.cursor if last is not None else 0):
                        raise ValueError("journal recovery checkpoint does not match stored prefix")
                    if last is not None:
                        cls._decode(last.payload, source_id, last.cursor)
                    state = RecoveryState(
                        checkpoint.through_cursor, checkpoint.harness_running, checkpoint.active_turn_id
                    )
                yield cls(connection, engine, source_id, state)
        finally:
            await engine.dispose()

    @staticmethod
    def _decode(payload: bytes, source_id: str, cursor: int) -> event_log_pb2.EventEntry:
        entry = event_log_pb2.EventEntry.FromString(payload)
        if (entry.cursor, entry.origin.source_id, entry.origin.sequence) != (cursor, source_id, cursor):
            raise ValueError("invalid stored runner EventEntry origin or cursor")
        return entry

    @property
    def recovery_state(self) -> RecoveryState:
        return self._state

    @property
    def last_cursor(self) -> int:
        return self._state.through_cursor

    async def since(self, after_cursor: int, *, limit: int) -> list[event_log_pb2.EventEntry]:
        through_cursor = self.last_cursor
        if not 0 <= after_cursor <= through_cursor or limit < 1:
            raise ValueError("replay requires an existing cursor and a positive page size")
        async with AsyncSession(self._engine) as session:
            rows = (
                await session.execute(
                    select(EventEntry.cursor, EventEntry.payload)
                    .where(EventEntry.cursor > after_cursor, EventEntry.cursor <= through_cursor)
                    .order_by(EventEntry.cursor)
                    .limit(limit)
                )
            ).all()
        result = []
        for expected, row in enumerate(rows, start=after_cursor + 1):
            if row.cursor != expected:
                raise ValueError("gap in stored runner EventEntry prefix")
            result.append(self._decode(row.payload, self.source_id, expected))
        if len(result) != min(limit, through_cursor - after_cursor):
            raise ValueError("incomplete stored runner EventEntry prefix")
        return result

    async def reached_checkpoint(self, name: str, command_id: str) -> bool:
        async with self._lock, AsyncSession(self._connection) as session:
            self._check_writable()
            return await session.get(DebugCheckpoint, (name, command_id)) is not None

    async def has_adapter_item(self, adapter_id: str, item_id: str) -> bool:
        async with self._lock, AsyncSession(self._connection) as session:
            self._check_writable()
            return await session.get(AdapterItem, (adapter_id, item_id)) is not None

    async def remember_adapter_item(self, adapter_id: str, item_id: str) -> bool:
        """Record a process-local item identity; return whether this is its first appearance."""
        async with self._transaction() as (session, _):
            if await session.get(AdapterItem, (adapter_id, item_id)) is not None:
                return False
            session.add(AdapterItem(adapter_id=adapter_id, item_id=item_id))
        return True

    async def next_adapter_block(self, adapter_id: str, message_id: str) -> int:
        """Allocate the next block index for a native message in this adapter instance."""
        async with self._transaction() as (session, _):
            message = await session.get(AdapterMessage, (adapter_id, message_id))
            if message is None:
                session.add(AdapterMessage(adapter_id=adapter_id, message_id=message_id, next_block=1))
                return 0
            index = message.next_block
            message.next_block += 1
            return index

    def _check_writable(self) -> None:
        if self._failure is not None:
            raise JournalStorageError("journal failed; reopen it for recovery") from self._failure

    async def wait_beyond(self, cursor: int) -> None:
        while self.last_cursor <= cursor:
            self._check_writable()
            await self._changed.wait()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[tuple[AsyncSession, list[event_log_pb2.EventEntry]]]:
        # aiosqlite serializes individual statements, not multi-await transactions. This lock
        # owns the connection through commit and publication, including cancellation recovery.
        async with self._lock:
            self._check_writable()
            appended: list[event_log_pb2.EventEntry] = []
            try:
                async with AsyncSession(self._connection, expire_on_commit=False) as session, session.begin():
                    # Disable sqlite3's legacy implicit-BEGIN rules and reserve the writer before
                    # reading command identities/cursors. Reads do not open native transactions.
                    await session.execute(text("BEGIN IMMEDIATE"))
                    yield session, appended
            except (SQLAlchemyError, JournalStorageError, asyncio.CancelledError) as error:
                self._failure = error
                self._changed.set()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise JournalStorageError("journal transaction failed") from error
            # Publish only after commit. A cancelled/failed commit may have reached storage, so
            # that writer stops and recovery reads SQLite's committed state.
            if appended:
                checkpoint = Checkpoint(
                    through_cursor=self.last_cursor,
                    harness_running=self._state.harness_running,
                    active_turn_id=self._state.active_turn_id,
                )
                for entry in appended:
                    _apply_checkpoint(checkpoint, entry)
                self._state = RecoveryState(
                    checkpoint.through_cursor, checkpoint.harness_running, checkpoint.active_turn_id
                )
                changed, self._changed = self._changed, asyncio.Event()
                changed.set()

    async def _append(
        self,
        session: AsyncSession,
        appended: list[event_log_pb2.EventEntry],
        observation: Observation,
        sources: Sequence[int],
    ) -> event_log_pb2.EventEntry:
        cursor = (await session.scalar(select(func.max(EventEntry.cursor))) or 0) + 1
        if cursor != self.last_cursor + len(appended) + 1:
            raise JournalStorageError("journal changed outside its owner; reopen it for recovery")
        event = observation_event(observation)
        event.source_sequences.extend(sources)
        event.at.FromDatetime(datetime.now(UTC))
        entry = event_log_pb2.EventEntry(
            cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=self.source_id, sequence=cursor), event=event
        )
        session.add(EventEntry(cursor=cursor, payload=entry.SerializeToString()))
        checkpoint = await session.get(Checkpoint, 1)
        if checkpoint is None or checkpoint.source_id != self.source_id or checkpoint.through_cursor != cursor - 1:
            raise JournalStorageError("journal recovery checkpoint changed outside its owner")
        _apply_checkpoint(checkpoint, entry)
        if event.HasField("debug_checkpoint"):
            debug = event.debug_checkpoint
            if await session.get(DebugCheckpoint, (debug.name, debug.command_id)) is None:
                session.add(DebugCheckpoint(name=debug.name, command_id=debug.command_id))
        await session.flush()
        appended.append(entry)
        return entry

    async def admit(self, command: command_pb2.Command) -> event_log_pb2.EventEntry | None:
        if not command.command_id or command.WhichOneof("operation") is None:
            raise ValueError("command requires command_id and operation")
        # Freeze the full caller-owned protobuf before the first await.
        command = command_pb2.Command.FromString(command.SerializeToString())
        async with self._transaction() as (session, appended):
            previous = await session.get(Command, command.command_id)
            if previous is not None:
                if command_pb2.Command.FromString(previous.payload) != command:
                    raise CommandConflictError(f"command id {command.command_id!r} was reused for different work")
                return None
            entry = await self._append(session, appended, event_pb2.CommandAdmitted(command=command), [])
            session.add(
                Command(
                    command_id=command.command_id, payload=command.SerializeToString(), admitted_cursor=entry.cursor
                )
            )
        return entry

    async def append(
        self,
        observation: Observation,
        *,
        sources: Sequence[int] = (),
        terminal_command_ids: Sequence[str] = (),
        native_correlation: dict[str, str] | None = None,
    ) -> event_log_pb2.EventEntry:
        async with self._transaction() as (session, appended):
            entry = await self._append(session, appended, observation, sources)
            for command_id in terminal_command_ids:
                command = await session.get(Command, command_id)
                if command is None:
                    raise ValueError(f"unknown command id {command_id!r}")
                if command.terminal_cursor is None:
                    command.terminal_cursor = entry.cursor
                    command.native_correlation = {**command.native_correlation, **(native_correlation or {})}
        return entry

    async def dispatch_planned(self, command_id: str, *, native_correlation: dict[str, str] | None = None) -> None:
        async with self._transaction() as (session, _):
            command = await session.get(Command, command_id)
            if command is None:
                raise ValueError(f"unknown command id {command_id!r}")
            if command.terminal_cursor is None:
                command.dispatch_planned = True
                command.native_correlation = {**command.native_correlation, **(native_correlation or {})}

    async def pending_commands(self) -> list[command_pb2.Command]:
        async with self._lock, AsyncSession(self._connection) as session:
            self._check_writable()
            payloads = await session.scalars(
                select(Command.payload).where(Command.terminal_cursor.is_(None)).order_by(Command.admitted_cursor)
            )
            return [command_pb2.Command.FromString(payload) for payload in payloads]

    async def get(self, command_id: str) -> Command | None:
        async with self._lock, AsyncSession(self._connection) as session:
            self._check_writable()
            return await session.get(Command, command_id)
