"""One session's command state and exact replayable Events, committed together."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

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


type _Transaction = tuple[AsyncSession, list[event_log_pb2.EventEntry]]


@dataclass(frozen=True)
class _Held:
    """A batch's open transaction: its context, still to be exited, and what that yielded."""

    context: AbstractAsyncContextManager[_Transaction]
    session: AsyncSession
    appended: list[event_log_pb2.EventEntry]


@dataclass
class _Batch:
    owner: asyncio.Task[object]
    # Open from the batch's first write until its next commit, holding the journal lock throughout.
    held: _Held | None = None


class CommandConflictError(ValueError):
    """One command id was reused with different requested work."""


class JournalStorageError(OSError):
    """Publication stopped; reopen the database before serving this source again."""


class Journal:
    def __init__(self, connection: AsyncConnection, source_id: str, state: RecoveryState) -> None:
        self._connection = connection
        self.source_id = source_id
        self._state = state
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._failure: BaseException | None = None
        self._batch: _Batch | None = None

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path, source_id: str) -> AsyncIterator[Journal]:
        # One journal lock owns both transactions and bounded replay reads. This avoids
        # holding a rollback-journal reader while a native callback commits an Event.
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}", poolclass=StaticPool, connect_args={"isolation_level": None}
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
                yield cls(connection, source_id, state)
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
        async with self._lock:
            task = asyncio.create_task(self._read_page(after_cursor, through_cursor, limit))
            try:
                rows = await asyncio.shield(task)
            except asyncio.CancelledError:
                # The bounded database read finishes and closes its session before this
                # cancellation releases the journal lock to a writer.
                await task
                raise
        result = []
        for expected, (cursor, payload) in enumerate(rows, start=after_cursor + 1):
            if cursor != expected:
                raise ValueError("gap in stored runner EventEntry prefix")
            result.append(self._decode(payload, self.source_id, expected))
        if len(result) != min(limit, through_cursor - after_cursor):
            raise ValueError("incomplete stored runner EventEntry prefix")
        return result

    async def _read_page(self, after_cursor: int, through_cursor: int, limit: int) -> list[tuple[int, bytes]]:
        async with AsyncSession(self._connection) as session:
            rows = await session.execute(
                select(EventEntry.cursor, EventEntry.payload)
                .where(EventEntry.cursor > after_cursor, EventEntry.cursor <= through_cursor)
                .order_by(EventEntry.cursor)
                .limit(limit)
            )
            return [(row.cursor, row.payload) for row in rows]

    async def reached_checkpoint(self, name: str, command_id: str) -> bool:
        async with self._reading() as session:
            return await session.get(DebugCheckpoint, (name, command_id)) is not None

    async def has_adapter_item(self, adapter_id: str, item_id: str) -> bool:
        async with self._reading() as session:
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
    async def batch(self) -> AsyncIterator[None]:
        """Hold every write the calling task makes in one transaction until `commit_batch` or the
        block's end, then commit and publish them together.

        Once the batch has written, every other writer and reader waits for its commit, so the task
        must commit before it awaits anything but the journal. A write that raises is rolled back to
        its savepoint and the batch goes on, as it would outside a batch. A storage failure fails the
        journal, as a failed commit does: none of the batch is published.
        """
        if self._batch is not None:
            raise RuntimeError("the journal already has a batch open")
        owner = asyncio.current_task()
        assert owner is not None
        batch = self._batch = _Batch(owner)
        try:
            yield
        except BaseException as error:
            if batch.held is not None:
                await self._abandon(batch, error)
            raise
        else:
            await self.commit_batch()
        finally:
            self._batch = None

    def batching(self) -> bool:
        """Whether the calling task has a batch open."""
        return self._own_batch() is not None

    async def commit_batch(self) -> None:
        """Commit and publish what the calling task's batch holds; later writes start another."""
        batch = self._own_batch()
        if batch is None:
            raise RuntimeError("the calling task has no batch open")
        if batch.held is not None:
            context, batch.held = batch.held.context, None
            await context.__aexit__(None, None, None)

    def _own_batch(self) -> _Batch | None:
        return self._batch if self._batch is not None and self._batch.owner is asyncio.current_task() else None

    async def _abandon(self, batch: _Batch, error: BaseException) -> None:
        """Roll back the batch's transaction. Its Events are never published, so the writer stops."""
        assert batch.held is not None
        context, batch.held = batch.held.context, None
        try:
            await context.__aexit__(type(error), error, error.__traceback__)
        finally:
            if self._failure is None:
                self._failure = error
            self._changed.set()

    @asynccontextmanager
    async def _reading(self) -> AsyncIterator[AsyncSession]:
        """A session for reads: the batch's own, which sees its uncommitted writes, while it holds one."""
        batch = self._own_batch()
        if batch is not None and batch.held is not None:
            yield batch.held.session
            return
        async with self._lock, AsyncSession(self._connection) as session:
            self._check_writable()
            yield session

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[_Transaction]:
        batch = self._own_batch()
        if batch is None:
            async with self._committed_transaction() as transaction:
                yield transaction
            return
        if batch.held is None:
            context = self._committed_transaction()
            session, appended = await context.__aenter__()
            batch.held = _Held(context, session, appended)
        held = batch.held
        before = len(held.appended)
        try:
            async with held.session.begin_nested():
                yield held.session, held.appended
        except (SQLAlchemyError, JournalStorageError, asyncio.CancelledError) as error:
            await self._abandon(batch, error)
            if isinstance(error, (JournalStorageError, asyncio.CancelledError)):
                raise
            raise JournalStorageError("journal batch failed") from error
        except BaseException:
            # The savepoint undid this write alone; the batch's earlier writes stand.
            del held.appended[before:]
            raise

    @asynccontextmanager
    async def _committed_transaction(self) -> AsyncIterator[_Transaction]:
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
        async with self._reading() as session:
            payloads = await session.scalars(
                select(Command.payload).where(Command.terminal_cursor.is_(None)).order_by(Command.admitted_cursor)
            )
            return [command_pb2.Command.FromString(payload) for payload in payloads]

    async def get(self, command_id: str) -> Command | None:
        async with self._reading() as session:
            return await session.get(Command, command_id)
