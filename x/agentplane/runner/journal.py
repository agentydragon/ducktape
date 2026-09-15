"""One session's command state and exact replayable Events, committed together."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, LargeBinary, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from x.agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from x.agentplane.runner.observation import Observation, observation_event

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


class CommandConflictError(ValueError):
    """One command id was reused with different requested work."""


class JournalStorageError(OSError):
    """Publication stopped; reopen the database before serving this source again."""


class Journal:
    def __init__(self, connection: AsyncConnection, source_id: str, entries: list[bytes]) -> None:
        self._connection = connection
        self.source_id = source_id
        self._entries = entries
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._failure: BaseException | None = None

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path, source_id: str) -> AsyncIterator[Journal]:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}", poolclass=StaticPool, connect_args={"isolation_level": None}
        )
        try:
            async with engine.connect() as connection:
                # EXTRA includes the rollback-journal unlink directory fence. These SQLite
                # controls are not application queries; the runtime constraint is in the design.
                await connection.execute(text("PRAGMA journal_mode=DELETE"))
                await connection.execute(text("PRAGMA synchronous=EXTRA"))
                await connection.execute(text("PRAGMA foreign_keys=ON"))
                await connection.commit()
                async with connection.begin():
                    await connection.run_sync(Base.metadata.create_all)
                async with AsyncSession(connection) as session:
                    entries = list(
                        (await session.scalars(select(EventEntry.payload).order_by(EventEntry.cursor))).all()
                    )
                for cursor, payload in enumerate(entries, start=1):
                    entry = event_log_pb2.EventEntry.FromString(payload)
                    if (entry.cursor, entry.origin.source_id, entry.origin.sequence) != (cursor, source_id, cursor):
                        raise ValueError("invalid stored runner EventEntry origin or cursor")
                yield cls(connection, source_id, entries)
        finally:
            await engine.dispose()

    @property
    def last_cursor(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[event_log_pb2.EventEntry]:
        return self.since(0)

    def since(self, after_cursor: int) -> list[event_log_pb2.EventEntry]:
        return [event_log_pb2.EventEntry.FromString(payload) for payload in self._entries[after_cursor:]]

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
                self._entries.extend(entry.SerializeToString() for entry in appended)
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
