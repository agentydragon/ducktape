"""Trajectories outlive sandboxes: every runner event, `Native` frames included, copied into
PostgreSQL as it arrives.

A product Thread is not a runner session. A `ThreadRunnerSession` records a proven association to
one runner session at one point in the Thread's life. `ThreadCommand` is the app-owned desired
side: one ordered, immutable command outbox per Thread. Events are stored as the protocol's own
proto-JSON, so a Thread reads back without a runner and a deleted Sandbox loses nothing. The schema
is owned by the Alembic migrations under `migrations/`, applied by `database_migrate.py` as a
separate deploy step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from x.agentplane.app.changes import Changes
from x.agentplane.app.operator_sessions import Base, OperatorSessionStore
from x.agentplane.app.presets import Harness
from x.agentplane.app.trajectory_updates import CHANNEL, COMMANDS_PAYLOAD, TrajectoryUpdates
from x.agentplane.protocol import command_pb2, event_log_pb2
from x.agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SQLAlchemy loads the asyncpg dialect from the URL scheme; nothing imports it directly.
# gazelle:include_dep @pypi//asyncpg


class Thread(Base):
    __tablename__ = "thread"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    # One Thread remains pinned to one Sandbox even when the harness is replaced. A future
    # Thread-start request is unbound until its reconciler creates this object.
    sandbox: Mapped[str | None] = mapped_column(Text)
    sandbox_uid: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    # A harness/spec projection survives between runner-session attachments. A later runner
    # session may change the live model projection, but its identity never becomes a Thread id.
    harness: Mapped[Harness] = mapped_column(
        SqlEnum(
            Harness,
            native_enum=False,
            create_constraint=False,
            values_callable=lambda values: [item.value for item in values],
        )
    )
    model: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    # NULL while unnamed; never the empty string.
    name: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ThreadRunnerSession(Base):
    """One runner-session identity within the Thread's static Sandbox."""

    __tablename__ = "thread_runner_session"
    __table_args__ = (
        Index("thread_runner_session_one_active", "thread_id", unique=True, postgresql_where=text("active")),
    )

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"))
    # Agentplane mints runner-session ids, so this global idempotency key needs no repeated Sandbox column.
    runner_session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # A Thread retains prior sessions but exposes exactly one current command target.
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Event(Base):
    __tablename__ = "event"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The observation's oneof case, for filtering without opening the payload; "native" for frames.
    kind: Mapped[str] = mapped_column(Text)
    # Proto-JSON of the protocol's EventEntry, exactly what the bridge streams.
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


class ThreadCommand(Base):
    """One immutable, ordered app-side command intent for a Thread."""

    __tablename__ = "thread_command"
    __table_args__ = (UniqueConstraint("thread_id", "ordinal"),)

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    # The runner's idempotency key is scoped to the durable Thread command log.
    command_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger)
    command: Mapped[dict[str, object]] = mapped_column(JSONB)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


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
    attached: protocol_pb2.Attached
    end: FeedEnd | FeedError | None


@dataclass(frozen=True)
class ThreadCommandSnapshot:
    thread_id: UUID
    command: command_pb2.Command
    ordinal: int
    accepted_at: datetime


@dataclass(frozen=True)
class ThreadCommandDelivery:
    """The next desired command for a Thread's active runner session."""

    command: ThreadCommandSnapshot
    runner_session_id: str


class ThreadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sandbox: str
    session_id: str
    harness: Harness = Field(description="The runner protocol Harness enum member.")
    model: str
    cwd: str
    created_at: datetime
    name: str | None = Field(description="The user-given name; None while the thread is unnamed.")
    archived: bool
    last_cursor: int = Field(description="The highest stored follow cursor; 0 while nothing is stored.")
    last_event_at: datetime | None = None
    harness_state: str = Field(
        description="The protocol's HarnessState enum member, by name: HARNESS_STATE_RUNNING, "
        "HARNESS_STATE_STOPPED, or HARNESS_STATE_UNSPECIFIED while no feed has ever attached to this thread."
    )


class ThreadNotFoundError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no thread {thread_id}")


class ThreadCommandConflictError(Exception):
    """A command id was reused with a different immutable command payload."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class TrajectoryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.operator_sessions = OperatorSessionStore(engine)
        self.changes = Changes()
        self.command_changes = Changes()
        self._updates = TrajectoryUpdates(engine.url, self.changes, self.command_changes)

    @classmethod
    def connect(cls, database_url: str) -> TrajectoryStore:
        return cls(create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True))

    async def close(self) -> None:
        await self._updates.close()
        await self._engine.dispose()

    async def start_updates(self) -> None:
        await self._updates.start()

    async def thread(
        self, sandbox: str, session_id: str, spec: protocol_pb2.SessionSpec, *, sandbox_uid: UUID | None = None
    ) -> UUID:
        """Find the product Thread for an actual runner attachment, creating it on first sight."""
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(Thread).join(ThreadRunnerSession).where(ThreadRunnerSession.runner_session_id == session_id)
            )
            if existing is not None:
                _match_thread_sandbox(existing, sandbox, sandbox_uid)
                if existing.sandbox_uid is None and sandbox_uid is not None:
                    existing.sandbox_uid = sandbox_uid
                    await _notify(session)
                return existing.id

            thread_id = uuid4()
            session.add(
                Thread(
                    id=thread_id,
                    sandbox=sandbox,
                    sandbox_uid=sandbox_uid,
                    harness=Harness(protocol_pb2.Harness.Name(spec.harness)),
                    model=spec.model,
                    cwd=spec.cwd,
                )
            )
            winner = await _activate_runner_session(session, thread_id, session_id)
            if winner != thread_id:
                await session.execute(delete(Thread).where(Thread.id == thread_id))
                winning_thread = await session.get(Thread, winner)
                if winning_thread is None:  # pragma: no cover - the session insert returned its thread id.
                    raise RuntimeError("runner-session insert conflicted without a winning Thread")
                _match_thread_sandbox(winning_thread, sandbox, sandbox_uid)
                return winner
            await session.flush()
            await _notify(session)
            return thread_id

    async def request_thread_command(self, thread_id: UUID, command: command_pb2.Command) -> ThreadCommandSnapshot:
        """Append a generic desired command, idempotently, to one existing Thread.

        The Thread row is locked before assigning its next ordinal, so concurrent app replicas
        establish one durable command order at commit time.
        """
        _validate_command(command)
        async with self._sessions.begin() as session:
            thread = await session.scalar(select(Thread).where(Thread.id == thread_id).with_for_update())
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            snapshot = await _append_thread_command(session, thread_id, command)
            await _notify(session, commands=True)
            return snapshot

    async def thread_commands(self, thread_id: UUID) -> list[ThreadCommandSnapshot]:
        """Read durable desired commands in the order a reconciler must consider them."""
        async with self._sessions() as session:
            commands = await session.scalars(
                select(ThreadCommand)
                .where(ThreadCommand.thread_id == thread_id)
                .order_by(ThreadCommand.ordinal, ThreadCommand.command_id)
            )
            return [_thread_command_snapshot(command) for command in commands]

    async def commands_awaiting_runner_admission(self, sandbox: str) -> list[ThreadCommandDelivery]:
        """The first non-admitted command for each Thread in one static Sandbox.

        The copied runner `CommandAdmitted` Event is the durable hand-off from app intent to
        runner admission. Later commands become eligible after admission, not after a native
        effect: the runner owns its own queued-command semantics and causal outcomes.
        """
        commands = (
            select(ThreadCommand, ThreadRunnerSession)
            .join(Thread, Thread.id == ThreadCommand.thread_id)
            .join(
                ThreadRunnerSession, (ThreadRunnerSession.thread_id == Thread.id) & ThreadRunnerSession.active.is_(True)
            )
            .where(Thread.sandbox == sandbox)
            .order_by(ThreadCommand.thread_id, ThreadCommand.ordinal, ThreadCommand.command_id)
        )
        admitted = (
            select(Event.thread_id, Event.payload)
            .join(Thread, Thread.id == Event.thread_id)
            .where(Thread.sandbox == sandbox, Event.kind == "command_admitted")
        )
        async with self._sessions() as session:
            admitted_ids = {
                (thread_id, ParseDict(payload, event_log_pb2.EventEntry()).event.command_admitted.command.command_id)
                for thread_id, payload in await session.execute(admitted)
            }
            deliveries: list[ThreadCommandDelivery] = []
            considered: set[UUID] = set()
            for command, runner_session in await session.execute(commands):
                if command.thread_id in considered:
                    continue
                if (command.thread_id, command.command_id) in admitted_ids:
                    continue
                considered.add(command.thread_id)
                deliveries.append(
                    ThreadCommandDelivery(
                        command=_thread_command_snapshot(command), runner_session_id=runner_session.runner_session_id
                    )
                )
            return deliveries

    async def last_cursor(self, thread_id: UUID) -> int:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(func.coalesce(func.max(Event.cursor), 0)).where(Event.thread_id == thread_id)
                )
                or 0
            )

    async def record(
        self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, lease: IngestionLease
    ) -> None:
        """Store entries; one already stored under its cursor is left as it was, so a replay after
        a reconnect is harmless."""
        if not entries:
            return
        rows = [
            {
                "thread_id": thread_id,
                "cursor": entry.cursor,
                "at": entry.event.at.ToDatetime(tzinfo=UTC),
                "kind": entry.event.WhichOneof("observation") or "",
                "payload": MessageToDict(entry),
            }
            for entry in entries
        ]
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            inserted = list(
                await session.scalars(insert(Event).values(rows).on_conflict_do_nothing().returning(Event.payload))
            )
            state = await session.get(FeedState, thread_id)
            if state is not None:
                attached = ParseDict(state.attached, protocol_pb2.Attached())
                previous_model = attached.spec.model
                for entry in sorted(
                    (ParseDict(payload, event_log_pb2.EventEntry()) for payload in inserted),
                    key=lambda entry: entry.cursor,
                ):
                    # An Attached snapshot describes the runner at its cursor. Replaying the
                    # earlier log fills history, but must not rewind that snapshot's state.
                    if entry.cursor <= attached.last_cursor:
                        continue
                    _project_attached(attached, entry)
                    if entry.event.HasField("harness_started"):
                        state.end = None
                state.attached = MessageToDict(attached)
                if attached.spec.model != previous_model:
                    await session.execute(
                        update(Thread).where(Thread.id == thread_id).values(model=attached.spec.model)
                    )
                await session.flush()
            await _notify(
                session,
                commands=any(
                    ParseDict(payload, event_log_pb2.EventEntry()).event.HasField("command_admitted")
                    for payload in inserted
                ),
            )

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

    async def set_attached(self, thread_id: UUID, attached: protocol_pb2.Attached, *, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            state = await session.get(FeedState, thread_id)
            if (
                state is not None
                and attached.last_cursor < ParseDict(state.attached, protocol_pb2.Attached()).last_cursor
            ):
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
            return FeedSnapshot(ParseDict(state.attached, protocol_pb2.Attached()), end)

    async def list_threads(
        self, *, sandbox: str | None = None, session_id: str | None = None, include_archived: bool = False
    ) -> list[ThreadView]:
        """Newest first; each filter given narrows the list to threads matching it. Archived
        threads are excluded unless asked for, mirroring the Sandbox inventory's own default."""
        query = _thread_views_query().order_by(Thread.created_at.desc())
        if sandbox is not None:
            query = query.where(Thread.sandbox == sandbox)
        if session_id is not None:
            query = query.where(ThreadRunnerSession.runner_session_id == session_id)
        if not include_archived:
            query = query.where(Thread.archived.is_(False))
        async with self._sessions() as session:
            return [
                _view(thread, runner, last_cursor, last_at, attached)
                for thread, runner, last_cursor, last_at, attached in await session.execute(query)
            ]

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            return await _thread_view(session, thread_id)

    async def rename(self, thread_id: UUID, name: str | None) -> ThreadView:
        """Set or, with None, clear the thread's name."""
        async with self._sessions.begin() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            thread.name = name
            await session.flush()
            renamed = await _thread_view(session, thread_id)
            if renamed is None:  # pragma: no cover - the locked row exists.
                raise RuntimeError("renamed Thread disappeared before it could be projected")
            await _notify(session)
        return renamed

    async def archive(self, thread_id: UUID) -> ThreadView:
        """Hide the thread from a default listing without touching its events."""
        return await self._set_archived(thread_id, True)

    async def unarchive(self, thread_id: UUID) -> ThreadView:
        return await self._set_archived(thread_id, False)

    async def _set_archived(self, thread_id: UUID, archived: bool) -> ThreadView:
        async with self._sessions.begin() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            thread.archived = archived
            await session.flush()
            view = await _thread_view(session, thread_id)
            if view is None:  # pragma: no cover - the locked row exists.
                raise RuntimeError("archived Thread disappeared before it could be projected")
            await _notify(session)
        return view

    async def events(self, thread_id: UUID, *, after_cursor: int = 0, limit: int) -> list[event_log_pb2.EventEntry]:
        """Up to `limit` entries after the cursor, in cursor order; a reader pages until a short page."""
        async with self._sessions() as session:
            payloads = await session.scalars(
                select(Event.payload)
                .where(Event.thread_id == thread_id, Event.cursor > after_cursor)
                .order_by(Event.cursor)
                .limit(limit)
            )
            return [ParseDict(payload, event_log_pb2.EventEntry()) for payload in payloads]


def _validate_command(command: command_pb2.Command) -> None:
    if not command.command_id or command.WhichOneof("operation") is None:
        raise ValueError("a Thread command needs a non-empty command id and operation")


async def _append_thread_command(
    session: AsyncSession, thread_id: UUID, command: command_pb2.Command
) -> ThreadCommandSnapshot:
    _validate_command(command)
    encoded = MessageToDict(command)
    existing = await session.get(ThreadCommand, (thread_id, command.command_id))
    if existing is not None:
        if existing.command != encoded:
            raise ThreadCommandConflictError("command id was already used for a different Thread command")
        return _thread_command_snapshot(existing)
    latest_ordinal = await session.scalar(
        select(func.coalesce(func.max(ThreadCommand.ordinal), 0)).where(ThreadCommand.thread_id == thread_id)
    )
    if latest_ordinal is None:  # SQL coalesce guarantees a row; retain an explicit typed invariant.
        raise RuntimeError("Thread command ordinal aggregate returned no value")
    row = ThreadCommand(
        thread_id=thread_id,
        command_id=command.command_id,
        ordinal=latest_ordinal + 1,
        command=encoded,
        accepted_at=datetime.now(UTC),
    )
    session.add(row)
    return _thread_command_snapshot(row)


def _thread_command_snapshot(command: ThreadCommand) -> ThreadCommandSnapshot:
    return ThreadCommandSnapshot(
        thread_id=command.thread_id,
        command=ParseDict(command.command, command_pb2.Command()),
        ordinal=command.ordinal,
        accepted_at=command.accepted_at,
    )


async def _activate_runner_session(session: AsyncSession, thread_id: UUID, runner_session_id: str) -> UUID:
    """Make this proven association the sole active target for its Thread."""
    await session.execute(
        update(ThreadRunnerSession)
        .where(ThreadRunnerSession.thread_id == thread_id, ThreadRunnerSession.active.is_(True))
        .values(active=False)
    )
    inserted = await session.scalar(
        insert(ThreadRunnerSession)
        .values(thread_id=thread_id, runner_session_id=runner_session_id, active=True)
        .on_conflict_do_nothing(index_elements=[ThreadRunnerSession.runner_session_id])
        .returning(ThreadRunnerSession.thread_id)
    )
    if inserted is not None:
        return inserted
    winner = await session.scalar(
        select(ThreadRunnerSession.thread_id).where(ThreadRunnerSession.runner_session_id == runner_session_id)
    )
    if winner is None:  # pragma: no cover - the conflict above names this unique key.
        raise RuntimeError("runner-session insert conflicted without a winning association")
    return winner


def _match_thread_sandbox(thread: Thread, sandbox: str, sandbox_uid: UUID | None) -> None:
    if thread.sandbox != sandbox:
        raise ValueError("runner session already belongs to a Thread pinned to a different Sandbox")
    if thread.sandbox_uid is not None and sandbox_uid is not None and thread.sandbox_uid != sandbox_uid:
        raise ValueError("runner session observed a different Kubernetes UID for its Thread Sandbox")


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


async def _notify(session: AsyncSession, *, commands: bool = False) -> None:
    # PostgreSQL delivers NOTIFY only on commit; payloads carry no trajectory or identity data.
    await session.execute(select(func.pg_notify(CHANNEL, COMMANDS_PAYLOAD if commands else "")))


def _project_attached(attached: protocol_pb2.Attached, entry: event_log_pb2.EventEntry) -> None:
    attached.last_cursor = entry.cursor
    event = entry.event
    match event.WhichOneof("observation"):
        case "harness_started":
            attached.harness_state = protocol_pb2.HARNESS_STATE_RUNNING
        case "harness_exited" | "harness_lost":
            attached.harness_state = protocol_pb2.HARNESS_STATE_STOPPED
        case "turn_started":
            attached.active_turn_id = event.turn_started.turn_id
        case "turn_completed":
            attached.active_turn_id = ""
        case "model_changed":
            attached.spec.model = event.model_changed.model


def _thread_views_query():
    """One current runner association plus durable transcript progress for each Thread."""
    last = (
        select(Event.thread_id, func.max(Event.cursor).label("last_cursor"), func.max(Event.at).label("last_at"))
        .group_by(Event.thread_id)
        .subquery()
    )
    return (
        select(Thread, ThreadRunnerSession, last.c.last_cursor, last.c.last_at, FeedState.attached)
        .join(ThreadRunnerSession, (ThreadRunnerSession.thread_id == Thread.id) & ThreadRunnerSession.active.is_(True))
        .outerjoin(last, last.c.thread_id == Thread.id)
        .outerjoin(FeedState, FeedState.thread_id == Thread.id)
    )


async def _thread_view(session: AsyncSession, thread_id: UUID) -> ThreadView | None:
    row = (await session.execute(_thread_views_query().where(Thread.id == thread_id))).one_or_none()
    if row is None:
        return None
    thread, runner, last_cursor, last_at, attached = row
    return _view(thread, runner, last_cursor, last_at, attached)


def _view(
    thread: Thread,
    runner: ThreadRunnerSession,
    last_cursor: int | None,
    last_at: datetime | None,
    attached: dict[str, object] | None,
) -> ThreadView:
    harness_state = (
        ParseDict(attached, protocol_pb2.Attached()).harness_state
        if attached is not None
        else protocol_pb2.HARNESS_STATE_UNSPECIFIED
    )
    if thread.sandbox is None:  # pragma: no cover - only a target-only Thread lacks an attachment.
        raise RuntimeError("Thread with a runner session has no pinned Sandbox")
    return ThreadView(
        id=thread.id,
        sandbox=thread.sandbox,
        session_id=runner.runner_session_id,
        harness=thread.harness,
        model=thread.model,
        cwd=thread.cwd,
        created_at=thread.created_at,
        name=thread.name,
        archived=thread.archived,
        last_cursor=last_cursor or 0,
        last_event_at=last_at,
        harness_state=protocol_pb2.HarnessState.Name(harness_state),
    )
