"""Trajectories outlive sandboxes: every runner event, `Native` frames included, copied into
PostgreSQL as it arrives.

A thread is one runner session, keyed by the sandbox and the client-chosen session id; its events
are stored as the protocol's own proto-JSON under the session's sequence, so a thread reads back
without a runner and a deleted sandbox loses nothing. The schema is created at startup: the store
is staging-only and disposable until a production instance needs migrations in place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from x.agentplane.app.changes import Changes
from x.agentplane.app.operator_sessions import Base as SessionBase, OperatorSessionStore
from x.agentplane.app.trajectory_updates import CHANNEL, TrajectoryUpdates
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


class SandboxIngestion(Base):
    __tablename__ = "sandbox_ingestion"

    sandbox: Mapped[str] = mapped_column(Text, primary_key=True)
    token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FeedState(Base):
    __tablename__ = "feed_state"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    attached: Mapped[dict[str, object]] = mapped_column(JSONB)
    # NULL means the stream has not ended. Empty JSON is a normal end; a message is an error end.
    end: Mapped[dict[str, str] | None] = mapped_column(JSONB(none_as_null=True))


@dataclass(frozen=True)
class IngestionLease:
    sandbox: str
    token: UUID


class IngestionLeaseLostError(Exception):
    """The sandbox ingester no longer owns authority to commit observations."""


@dataclass(frozen=True)
class FeedEnd:
    pass


@dataclass(frozen=True)
class FeedError:
    message: str


@dataclass(frozen=True)
class FeedSnapshot:
    attached: pb.Attached
    end: FeedEnd | FeedError | None


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
        self.operator_sessions = OperatorSessionStore(engine)
        self.changes = Changes()
        self._updates = TrajectoryUpdates(engine.url, self.changes)

    @classmethod
    def connect(cls, database_url: str) -> TrajectoryStore:
        return cls(create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True))

    async def ensure_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("SELECT pg_advisory_xact_lock(5820)"))
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(SessionBase.metadata.create_all)
            # create_all only creates tables it does not find; a column added since a table was
            # created is added here, idempotently, until the store grows a migration mechanism.
            await connection.execute(text("ALTER TABLE thread ADD COLUMN IF NOT EXISTS name text"))

    async def close(self) -> None:
        await self._updates.close()
        await self._engine.dispose()

    async def start_updates(self) -> None:
        await self._updates.start()

    async def thread(self, sandbox: str, session_id: str, spec: pb.SessionSpec) -> UUID:
        """The thread for a session, created from its spec on first sight."""
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(Thread)
                .values(
                    sandbox=sandbox,
                    session_id=session_id,
                    provider=pb.Provider.Name(spec.provider),
                    model=spec.model,
                    cwd=spec.cwd,
                )
                .on_conflict_do_nothing(index_elements=[Thread.sandbox, Thread.session_id])
                .returning(Thread.id)
            )
            if created is not None:
                await _notify(session)
                return created
            return (
                await session.scalars(
                    select(Thread.id).where(Thread.sandbox == sandbox, Thread.session_id == session_id)
                )
            ).one()

    async def last_sequence(self, thread_id: UUID) -> int:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(func.coalesce(func.max(Event.sequence), 0)).where(Event.thread_id == thread_id)
                )
                or 0
            )

    async def record(self, thread_id: UUID, events: Sequence[pb.Event], *, lease: IngestionLease) -> None:
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
            await _fence(session, lease, thread_id)
            inserted = await session.scalars(
                insert(Event).values(rows).on_conflict_do_nothing().returning(Event.payload)
            )
            state = await session.get(FeedState, thread_id)
            if state is not None:
                attached = ParseDict(state.attached, pb.Attached())
                previous_model = attached.spec.model
                for event in sorted(
                    (ParseDict(payload, pb.Event()) for payload in inserted), key=lambda event: event.sequence
                ):
                    # An Attached snapshot describes the runner at its cursor. Replaying the
                    # earlier log fills history, but must not rewind that snapshot's state.
                    if event.sequence <= attached.last_sequence:
                        continue
                    _project_attached(attached, event)
                    if event.HasField("harness_started"):
                        state.end = None
                state.attached = MessageToDict(attached)
                if attached.spec.model != previous_model:
                    await session.execute(
                        update(Thread).where(Thread.id == thread_id).values(model=attached.spec.model)
                    )
                await session.flush()
            await _notify(session)

    async def acquire_ingestion(self, sandbox: str, duration: timedelta) -> IngestionLease | None:
        _positive_duration(duration)
        token = uuid4()
        async with self._sessions.begin() as session:
            acquired = await session.scalar(
                insert(SandboxIngestion)
                .values(sandbox=sandbox, token=token, expires_at=func.clock_timestamp() + duration)
                .on_conflict_do_update(
                    index_elements=[SandboxIngestion.sandbox],
                    set_={"token": token, "expires_at": func.clock_timestamp() + duration},
                    where=SandboxIngestion.expires_at <= func.clock_timestamp(),
                )
                .returning(SandboxIngestion.token)
            )
        return IngestionLease(sandbox, token) if acquired is not None else None

    async def renew_ingestion(self, lease: IngestionLease, duration: timedelta) -> bool:
        _positive_duration(duration)
        async with self._sessions.begin() as session:
            renewed = await session.scalar(
                update(SandboxIngestion)
                .where(
                    SandboxIngestion.sandbox == lease.sandbox,
                    SandboxIngestion.token == lease.token,
                    SandboxIngestion.expires_at > func.clock_timestamp(),
                )
                .values(expires_at=func.clock_timestamp() + duration)
                .returning(SandboxIngestion.token)
            )
        return renewed is not None

    async def release_ingestion(self, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(SandboxIngestion).where(
                    SandboxIngestion.sandbox == lease.sandbox, SandboxIngestion.token == lease.token
                )
            )

    async def set_attached(self, thread_id: UUID, attached: pb.Attached, *, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            state = await session.get(FeedState, thread_id)
            if state is not None and attached.last_sequence < ParseDict(state.attached, pb.Attached()).last_sequence:
                raise ValueError("attachment snapshot is older than the committed feed state")
            values = {"attached": MessageToDict(attached), "end": None}
            await session.execute(
                insert(FeedState)
                .values(thread_id=thread_id, **values)
                .on_conflict_do_update(index_elements=[FeedState.thread_id], set_=values)
            )
            await session.execute(update(Thread).where(Thread.id == thread_id).values(model=attached.spec.model))
            await _notify(session)

    async def end_feed(self, thread_id: UUID, *, lease: IngestionLease, error: str | None) -> None:
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            state = await session.get(FeedState, thread_id)
            if state is None:
                raise ValueError("cannot end a feed before persisting its attachment")
            state.end = {} if error is None else {"message": error}
            await session.flush()
            await _notify(session)

    async def feed_state(self, thread_id: UUID) -> FeedSnapshot | None:
        async with self._sessions() as session:
            state = await session.get(FeedState, thread_id)
            if state is None:
                return None
            end = None if state.end is None else FeedError(state.end["message"]) if state.end else FeedEnd()
            return FeedSnapshot(ParseDict(state.attached, pb.Attached()), end)

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
            await _notify(session)
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


def _positive_duration(duration: timedelta) -> None:
    if duration <= timedelta(0):
        raise ValueError("ingestion lease duration must be positive")


async def _fence(session: AsyncSession, lease: IngestionLease, thread_id: UUID) -> None:
    # Lock before reading database time: a transaction that waited on an owner must not rely on
    # its transaction-start timestamp. Takeover/renewal waits until this write commits or rolls back.
    owned = await session.scalar(
        select(SandboxIngestion).where(SandboxIngestion.sandbox == lease.sandbox).with_for_update()
    )
    now = (await session.scalars(select(func.clock_timestamp()))).one()
    if owned is None or owned.token != lease.token or owned.expires_at <= now:
        raise IngestionLeaseLostError(lease.sandbox)
    sandbox = await session.scalar(select(Thread.sandbox).where(Thread.id == thread_id))
    if sandbox != lease.sandbox:
        raise IngestionLeaseLostError("lease does not own this thread's sandbox")


async def _notify(session: AsyncSession) -> None:
    # PostgreSQL delivers NOTIFY only on commit; payloads carry no trajectory or identity data.
    await session.execute(select(func.pg_notify(CHANNEL, "")))


def _project_attached(attached: pb.Attached, event: pb.Event) -> None:
    attached.last_sequence = event.sequence
    match event.WhichOneof("observation"):
        case "harness_started":
            attached.harness = pb.HARNESS_STATE_RUNNING
        case "harness_exited" | "harness_lost":
            attached.harness = pb.HARNESS_STATE_STOPPED
        case "turn_started":
            attached.active_turn_id = event.turn_started.turn_id
        case "turn_completed":
            attached.active_turn_id = ""
        case "model_switch_succeeded":
            attached.spec.model = event.model_switch_succeeded.model


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
