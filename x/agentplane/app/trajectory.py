"""Trajectories outlive sandboxes: every runner event, `Native` frames included, copied into
PostgreSQL as it arrives.

A thread is one runner session, keyed by the sandbox and the client-chosen session id; its events
are stored as the protocol's own proto-JSON under the session's sequence, so a thread reads back
without a runner and a deleted sandbox loses nothing. The schema is created at startup: the store
is staging-only and disposable until a production instance needs migrations in place.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from x.agentplane.app.changes import Changes
from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SQLAlchemy loads the asyncpg dialect from the URL scheme; nothing imports it directly.
# gazelle:include_dep @pypi//asyncpg


class Base(DeclarativeBase):
    pass


class Thread(Base):
    __tablename__ = "thread"
    __table_args__ = (UniqueConstraint("sandbox", "session_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    sandbox: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    # NULL while unnamed; never the empty string.
    name: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "event"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The observation's oneof case, for filtering without opening the payload; "native" for frames.
    kind: Mapped[str] = mapped_column(Text)
    # Proto-JSON of the protocol's Event, exactly what the bridge streams.
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class ThreadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sandbox: str
    session_id: str
    provider: str = Field(description="The protocol's Provider enum member, by name: PROVIDER_CLAUDE, PROVIDER_CODEX.")
    model: str
    cwd: str
    created_at: datetime
    name: str | None = Field(description="The user-given name; None while the thread is unnamed.")
    last_sequence: int = Field(description="The highest stored sequence; 0 while nothing is stored.")
    last_event_at: datetime | None = None


class ThreadNotFoundError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no thread {thread_id}")


class TrajectoryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        # A thread appearing or being renamed is a change the live stream pushes (live.py). Stored
        # events are not: they arrive by the hundreds per turn, and the session's own SSE carries
        # them already.
        self.changes = Changes()

    @classmethod
    def connect(cls, database_url: str) -> TrajectoryStore:
        return cls(create_async_engine(database_url, pool_pre_ping=True))

    async def ensure_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # create_all only creates tables it does not find; a column added since a table was
            # created is added here, idempotently, until the store grows a migration mechanism.
            await connection.execute(text("ALTER TABLE thread ADD COLUMN IF NOT EXISTS name text"))

    async def close(self) -> None:
        await self._engine.dispose()

    async def thread(self, sandbox: str, session_id: str, spec: pb.SessionSpec) -> UUID:
        """The thread for a session, created from its spec on first sight."""
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(Thread.id).where(Thread.sandbox == sandbox, Thread.session_id == session_id)
            )
            if existing is not None:
                return existing
            row = Thread(
                sandbox=sandbox,
                session_id=session_id,
                provider=pb.Provider.Name(spec.provider),
                model=spec.model,
                cwd=spec.cwd,
            )
            session.add(row)
            await session.flush()
            created = row.id
        # Past the commit: a subscriber woken here reads a thread the database already holds.
        self.changes.notify()
        return created

    async def last_sequence(self, thread_id: UUID) -> int:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(func.coalesce(func.max(Event.sequence), 0)).where(Event.thread_id == thread_id)
                )
                or 0
            )

    async def record(self, thread_id: UUID, events: Sequence[pb.Event]) -> None:
        """Store events; one already stored under its sequence is left as it was, so a replay after
        a reconnect is harmless."""
        if not events:
            return
        rows = [
            {
                "thread_id": thread_id,
                "sequence": event.sequence,
                "at": event.at.ToDatetime(tzinfo=UTC),
                "kind": event.WhichOneof("observation") or "",
                "payload": MessageToDict(event),
            }
            for event in events
        ]
        async with self._sessions.begin() as session:
            await session.execute(insert(Event).values(rows).on_conflict_do_nothing())

    async def list_threads(self, *, sandbox: str | None = None, session_id: str | None = None) -> list[ThreadView]:
        """Newest first; each filter given narrows the list to threads matching it."""
        last = (
            select(
                Event.thread_id, func.max(Event.sequence).label("last_sequence"), func.max(Event.at).label("last_at")
            )
            .group_by(Event.thread_id)
            .subquery()
        )
        query = (
            select(Thread, last.c.last_sequence, last.c.last_at)
            .outerjoin(last, last.c.thread_id == Thread.id)
            .order_by(Thread.created_at.desc())
        )
        if sandbox is not None:
            query = query.where(Thread.sandbox == sandbox)
        if session_id is not None:
            query = query.where(Thread.session_id == session_id)
        async with self._sessions() as session:
            return [
                _view(thread, last_sequence, last_at) for thread, last_sequence, last_at in await session.execute(query)
            ]

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                return None
            return _view(thread, *await _last(session, thread_id))

    async def rename(self, thread_id: UUID, name: str | None) -> ThreadView:
        """Set or, with None, clear the thread's name."""
        async with self._sessions.begin() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            thread.name = name
            await session.flush()
            renamed = _view(thread, *await _last(session, thread_id))
        self.changes.notify()
        return renamed

    async def events(self, thread_id: UUID, *, after_sequence: int = 0, limit: int) -> list[pb.Event]:
        """Up to `limit` events after the cursor, in sequence order; a reader pages until a short page."""
        async with self._sessions() as session:
            payloads = await session.scalars(
                select(Event.payload)
                .where(Event.thread_id == thread_id, Event.sequence > after_sequence)
                .order_by(Event.sequence)
                .limit(limit)
            )
            return [ParseDict(payload, pb.Event()) for payload in payloads]


async def _last(session: AsyncSession, thread_id: UUID) -> tuple[int | None, datetime | None]:
    last = await session.execute(
        select(func.max(Event.sequence), func.max(Event.at)).where(Event.thread_id == thread_id)
    )
    last_sequence, last_at = last.one()
    return last_sequence, last_at


def _view(thread: Thread, last_sequence: int | None, last_at: datetime | None) -> ThreadView:
    return ThreadView(
        id=thread.id,
        sandbox=thread.sandbox,
        session_id=thread.session_id,
        provider=thread.provider,
        model=thread.model,
        cwd=thread.cwd,
        created_at=thread.created_at,
        name=thread.name,
        last_sequence=last_sequence or 0,
        last_event_at=last_at,
    )
