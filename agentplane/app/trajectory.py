"""Trajectories outlive sandboxes: every runner event, `Native` frames included, copied into
PostgreSQL as it arrives.

A thread is one runner session, keyed by the sandbox and the client-chosen session id; its entries
are stored as the protocol's own proto-JSON under the source's follow cursor, so a thread reads back
without a runner and a deleted sandbox loses nothing. The schema is owned by the Alembic migrations
under `migrations/`, applied by `database_migrate.py` as a separate deploy step.
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

from agentplane.app import conversation_projection
from agentplane.app.changes import Changes
from agentplane.app.operator_sessions import Base, OperatorSessionStore
from agentplane.app.presets import Harness
from agentplane.app.trajectory_updates import CHANNEL, TrajectoryUpdates
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SQLAlchemy loads the asyncpg dialect from the URL scheme; nothing imports it directly.
# gazelle:include_dep @pypi//asyncpg


CONVERSATION_PROJECTION_EPOCH = "v1"


class Thread(Base):
    __tablename__ = "thread"
    __table_args__ = (UniqueConstraint("sandbox", "session_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    sandbox: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
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


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        Index("ix_event_thread_at", "thread_id", "at"),
        Index("ix_event_thread_origin", "thread_id", "origin_source_id", "origin_sequence"),
    )

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    origin_source_id: Mapped[str] = mapped_column(Text)
    origin_sequence: Mapped[int] = mapped_column(BigInteger)
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


class ConversationProjectionCheckpoint(Base):
    """One source/epoch-owned materialized prefix for a Thread."""

    __tablename__ = "conversation_projection_checkpoint"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text)
    projection_epoch: Mapped[str] = mapped_column(Text)
    through_cursor: Mapped[int] = mapped_column(BigInteger)


class ConversationEntity(Base):
    """The mutable, tagged current row consumed by the conversation view shape."""

    __tablename__ = "conversation_entity"
    __table_args__ = (
        Index("ix_conversation_entity_scope_revision", "thread_id", "source_id", "projection_epoch", "revision_cursor"),
        Index(
            "ix_conversation_entity_scope_cursor",
            "thread_id",
            "source_id",
            "projection_epoch",
            "cursor",
            "entity_kind",
            "entity_id",
        ),
        Index(
            "ix_conversation_entity_scope_pending_cursor",
            "thread_id",
            "source_id",
            "projection_epoch",
            "pending",
            "cursor",
            "entity_kind",
            "entity_id",
        ),
    )

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger)
    revision_cursor: Mapped[int] = mapped_column(BigInteger)
    pending: Mapped[bool] = mapped_column(Boolean)
    turn_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[dict[str, object]] = mapped_column(JSONB)
    text_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    arguments_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    output_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    input_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))


class ConversationPayloadManifest(Base):
    """An immutable exact field revision; chunks are owned by its generation."""

    __tablename__ = "conversation_payload_manifest"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    present: Mapped[bool] = mapped_column(Boolean)
    chunk_count: Mapped[int] = mapped_column(BigInteger)
    content_bytes: Mapped[int] = mapped_column(BigInteger)


class ConversationPayloadChunk(Base):
    """A UTF-8 fragment, immutable within a payload generation."""

    __tablename__ = "conversation_payload_chunk"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chunk_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class ConversationProjectionEvidence(Base):
    __tablename__ = "conversation_projection_evidence"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class ConversationProjectionNativeLink(Base):
    __tablename__ = "conversation_projection_native_link"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)


@dataclass(frozen=True)
class IngestionLease:
    sandbox: str
    token: UUID


class IngestionLeaseLostError(Exception):
    """The sandbox ingester no longer owns authority to commit observations."""


class EventReplicationError(ValueError):
    """The runner stream conflicts with the archived prefix or skips an entry."""


class ConversationProjectionError(EventReplicationError):
    """A semantic observation could not advance the durable conversation projection."""


class CommandIdConflictError(ValueError):
    """A Thread command id was already admitted with a different immutable Command."""


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
class ConversationScope:
    source_id: str
    projection_epoch: str
    through_cursor: int


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

    async def close(self) -> None:
        await self._updates.close()
        await self._engine.dispose()

    async def start_updates(self) -> None:
        await self._updates.start()

    @property
    def updates_connected(self) -> bool:
        return self._updates.connected

    async def thread(self, sandbox: str, session_id: str, spec: protocol_pb2.SessionSpec) -> UUID:
        """The thread for a session, created from its spec on first sight."""
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(Thread)
                .values(
                    sandbox=sandbox,
                    session_id=session_id,
                    harness=Harness(protocol_pb2.Harness.Name(spec.harness)),
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

    async def last_cursor(self, thread_id: UUID) -> int:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(Event.cursor).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
                )
                or 0
            )

    async def current_conversation_scope(self, thread_id: UUID) -> ConversationScope | None:
        """The sole source/epoch scope currently materialized for a Thread."""
        async with self._sessions() as session:
            checkpoint = await session.scalar(
                select(ConversationProjectionCheckpoint).where(ConversationProjectionCheckpoint.thread_id == thread_id)
            )
            if checkpoint is None:
                return None
            return ConversationScope(checkpoint.source_id, checkpoint.projection_epoch, checkpoint.through_cursor)

    async def record(
        self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, lease: IngestionLease
    ) -> None:
        """Atomically extend the contiguous prefix, accepting only identical replayed entries."""
        if not entries:
            return
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            last = await session.scalar(
                select(Event).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
            )
            cursor = last.cursor if last is not None else 0
            source_id = (
                ParseDict(last.payload, event_log_pb2.EventEntry()).origin.source_id if last is not None else None
            )
            payloads = dict(
                (
                    await session.execute(
                        select(Event.cursor, Event.payload).where(
                            Event.thread_id == thread_id,
                            Event.cursor.in_([entry.cursor for entry in entries if entry.cursor <= cursor]),
                        )
                    )
                )
                .tuples()
                .all()
            )
            inserted: list[event_log_pb2.EventEntry] = []
            for entry in entries:
                if not entry.cursor or not entry.origin.source_id or entry.origin.sequence != entry.cursor:
                    raise EventReplicationError(f"invalid runner origin at cursor {entry.cursor}")
                if source_id is not None and entry.origin.source_id != source_id:
                    raise EventReplicationError(f"runner source changed at cursor {entry.cursor}")
                payload = MessageToDict(entry)
                if entry.cursor in payloads:
                    if payloads[entry.cursor] != payload:
                        raise EventReplicationError(f"conflicting runner entry at cursor {entry.cursor}")
                    continue
                if entry.cursor != cursor + 1:
                    raise EventReplicationError(f"expected runner cursor {cursor + 1}, received {entry.cursor}")
                session.add(
                    Event(
                        thread_id=thread_id,
                        cursor=entry.cursor,
                        origin_source_id=entry.origin.source_id,
                        origin_sequence=entry.origin.sequence,
                        at=entry.event.at.ToDatetime(tzinfo=UTC),
                        kind=entry.event.WhichOneof("observation") or "",
                        payload=payload,
                    )
                )
                payloads[entry.cursor] = payload
                inserted.append(entry)
                cursor = entry.cursor
                source_id = entry.origin.source_id
            if not inserted:
                return
            try:
                await _record_conversation_projection(session, thread_id, inserted[0].origin.source_id, inserted)
            except ConversationProjectionError:
                raise
            except ValueError as error:
                cursor = (
                    error.cursor
                    if isinstance(error, conversation_projection.ObservationNotUnderstoodError)
                    else inserted[-1].cursor
                )
                raise ConversationProjectionError(
                    f"conversation projection failed at cursor {cursor}: {error}"
                ) from error
            # The maximum stored cursor is the checkpoint: the fenced transaction admits
            # only a contiguous suffix, so there is no separately mutable progress counter.
            state = await session.get(FeedState, thread_id)
            if state is not None:
                attached = ParseDict(state.attached, protocol_pb2.Attached())
                previous_model = attached.spec.model
                for entry in inserted:
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
        last_cursor = (
            select(Event.cursor)
            .where(Event.thread_id == Thread.id)
            .order_by(Event.cursor.desc())
            .limit(1)
            .scalar_subquery()
        )
        last_at = (
            select(Event.at).where(Event.thread_id == Thread.id).order_by(Event.at.desc()).limit(1).scalar_subquery()
        )
        query = (
            select(Thread, last_cursor, last_at, FeedState.attached)
            .outerjoin(FeedState, FeedState.thread_id == Thread.id)
            .order_by(Thread.created_at.desc())
        )
        if sandbox is not None:
            query = query.where(Thread.sandbox == sandbox)
        if session_id is not None:
            query = query.where(Thread.session_id == session_id)
        if not include_archived:
            query = query.where(Thread.archived.is_(False))
        async with self._sessions() as session:
            return [
                _view(thread, last_cursor, last_at, attached)
                for thread, last_cursor, last_at, attached in await session.execute(query)
            ]

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                return None
            return _view(thread, *await _last(session, thread_id))

    async def admitted_command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry | None:
        """The archived admission of this immutable command, if the Thread has one.

        This is deliberately an archive lookup rather than a command outbox. A matching result
        lets a retry recover a lost HTTP response without contacting a possibly deleted Sandbox;
        a reused id with different work is a conflict, never an implicit new command.
        """
        async with self._sessions() as session:
            if await session.get(Thread, thread_id) is None:
                raise ThreadNotFoundError(thread_id)
            payloads = await session.scalars(
                select(Event.payload)
                .where(
                    Event.thread_id == thread_id,
                    Event.kind == "command_admitted",
                    Event.payload["event"]["commandAdmitted"]["command"]["commandId"].as_string() == command.command_id,
                )
                .order_by(Event.cursor)
            )
            for payload in payloads:
                entry = ParseDict(payload, event_log_pb2.EventEntry())
                admitted = entry.event.command_admitted.command
                if admitted == command:
                    return entry
                raise CommandIdConflictError(
                    f"command id {command.command_id!r} was already admitted with different work"
                )
            return None

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
            view = _view(thread, *await _last(session, thread_id))
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


async def _record_conversation_projection(
    session: AsyncSession, thread_id: UUID, source_id: str, entries: Sequence[event_log_pb2.EventEntry]
) -> None:
    checkpoint = await session.scalar(
        select(ConversationProjectionCheckpoint)
        .where(ConversationProjectionCheckpoint.thread_id == thread_id)
        .with_for_update()
    )
    if checkpoint is None:
        state = conversation_projection.initial(source_id, CONVERSATION_PROJECTION_EPOCH)
    else:
        if checkpoint.source_id != source_id:
            raise EventReplicationError(f"conversation source changed at cursor {entries[0].cursor}")
        if checkpoint.projection_epoch != CONVERSATION_PROJECTION_EPOCH:
            raise ConversationProjectionError(
                f"conversation projection epoch {checkpoint.projection_epoch!r} must be reset for "
                f"{CONVERSATION_PROJECTION_EPOCH!r}"
            )
        state = await _conversation_state(session, checkpoint)
    batch = conversation_projection.EventBatch(source_id, state.position.through_cursor, tuple(entries))
    result = conversation_projection.advance(
        state, batch, await _prior_conversation_entities(session, thread_id, batch)
    )
    await _write_conversation_payloads(session, thread_id, result.payload_writes)
    for values in _conversation_entities(thread_id, result):
        await session.execute(
            insert(ConversationEntity)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    ConversationEntity.thread_id,
                    ConversationEntity.source_id,
                    ConversationEntity.projection_epoch,
                    ConversationEntity.entity_kind,
                    ConversationEntity.entity_id,
                ],
                set_=values,
            )
        )
    for evidence in result.evidence_upserts:
        values = {
            "thread_id": thread_id,
            "source_id": evidence.source_id,
            "projection_epoch": evidence.projection_epoch,
            "entity_cursor": evidence.entity_cursor,
            "observation_cursor": evidence.observation_cursor,
        }
        await session.execute(insert(ConversationProjectionEvidence).values(**values).on_conflict_do_nothing())
        for source_sequence in evidence.source_sequences:
            await session.execute(
                insert(ConversationProjectionNativeLink)
                .values(**values, source_sequence=source_sequence)
                .on_conflict_do_nothing()
            )
    checkpoint_values = {
        "source_id": result.state.position.source_id,
        "projection_epoch": result.state.position.projection_epoch,
        "through_cursor": result.state.position.through_cursor,
    }
    await session.execute(
        insert(ConversationProjectionCheckpoint)
        .values(thread_id=thread_id, **checkpoint_values)
        .on_conflict_do_update(index_elements=[ConversationProjectionCheckpoint.thread_id], set_=checkpoint_values)
    )


async def _conversation_state(
    session: AsyncSession, checkpoint: ConversationProjectionCheckpoint
) -> conversation_projection.ViewState:
    row = await session.scalar(
        select(ConversationEntity).where(
            ConversationEntity.thread_id == checkpoint.thread_id,
            ConversationEntity.source_id == checkpoint.source_id,
            ConversationEntity.projection_epoch == checkpoint.projection_epoch,
            ConversationEntity.entity_kind == "view_state",
            ConversationEntity.entity_id == "current",
        )
    )
    if row is None:
        raise ValueError("conversation checkpoint has no current controls")
    controls = row.state["controls"]
    if not isinstance(controls, dict):
        raise ValueError("conversation controls are invalid")
    return conversation_projection.ViewState(
        conversation_projection.Position(checkpoint.source_id, checkpoint.projection_epoch, checkpoint.through_cursor),
        conversation_projection.Controls(
            applied_model=_optional_str(controls, "applied_model"),
            active_turn_id=_optional_str(controls, "active_turn_id"),
            harness_state=_optional_str(controls, "harness_state"),
        ),
        _json_int(row.state, "unresolved_count"),
    )


async def _prior_conversation_entities(
    session: AsyncSession, thread_id: UUID, batch: conversation_projection.EventBatch
) -> conversation_projection.PriorEntities:
    required = conversation_projection.touched_keys(batch)
    checkpoint = await session.scalar(
        select(ConversationProjectionCheckpoint).where(ConversationProjectionCheckpoint.thread_id == thread_id)
    )
    if checkpoint is None:
        return conversation_projection.PriorEntities(
            dict.fromkeys(required.item_ids), dict.fromkeys(required.command_ids)
        )
    rows = await session.scalars(
        select(ConversationEntity).where(
            ConversationEntity.thread_id == thread_id,
            ConversationEntity.source_id == checkpoint.source_id,
            ConversationEntity.projection_epoch == checkpoint.projection_epoch,
            (
                (ConversationEntity.entity_kind == "item") & (ConversationEntity.entity_id.in_(required.item_ids))
                | (ConversationEntity.entity_kind == "command")
                & (ConversationEntity.entity_id.in_(required.command_ids))
            ),
        )
    )
    items: dict[str, conversation_projection.ConversationItem | None] = dict.fromkeys(required.item_ids)
    commands: dict[str, conversation_projection.CommandSummary | None] = dict.fromkeys(required.command_ids)
    for row in rows:
        if row.entity_kind == "item":
            items[row.entity_id] = _conversation_item(row)
        elif row.entity_kind == "command":
            commands[row.entity_id] = _command_summary(row)
    return conversation_projection.PriorEntities(items, commands)


def _conversation_item(row: ConversationEntity) -> conversation_projection.ConversationItem:
    return conversation_projection.ConversationItem(
        row.source_id,
        row.projection_epoch,
        row.entity_id,
        row.cursor,
        row.revision_cursor,
        kind=_json_int(row.state, "kind"),
        tool_name=_json_str(row.state, "tool_name"),
        turn_id=row.turn_id,
        text=_field_value(row.text_ref),
        arguments=_field_value(row.arguments_ref),
        output=_field_value(row.output_ref),
        completion=_optional_str(row.state, "completion"),
        tool_succeeded=_optional_bool(row.state, "tool_succeeded"),
    )


def _command_summary(row: ConversationEntity) -> conversation_projection.CommandSummary:
    outcome = _json_str(row.state, "outcome")
    return conversation_projection.CommandSummary(
        row.source_id,
        row.projection_epoch,
        row.entity_id,
        row.cursor,
        _json_str(row.state, "operation"),
        conversation_projection.CommandOutcome(outcome),
        _optional_json_int(row.state, "outcome_cursor"),
        _optional_str(row.state, "outcome_reason"),
        _field_value(row.input_ref),
    )


def _field_value(value: dict[str, object] | None) -> conversation_projection.FieldValue | None:
    return conversation_projection.FieldValue(_payload_ref_from_json(value)) if value is not None else None


def _payload_ref_from_json(value: dict[str, object]) -> conversation_projection.PayloadRef:
    return conversation_projection.PayloadRef(
        _json_str(value, "source_id"),
        _json_str(value, "projection_epoch"),
        _json_int(value, "owner_cursor"),
        _json_str(value, "owner_item_id"),
        conversation_projection.PayloadField(_json_str(value, "field")),
        _json_int(value, "revision_cursor"),
        _json_int(value, "generation"),
    )


def _json_str(value: dict[str, object], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise ValueError(f"conversation JSON {key} is not a string")
    return item


def _optional_str(value: dict[str, object], key: str) -> str | None:
    item = value[key]
    if item is not None and not isinstance(item, str):
        raise ValueError(f"conversation JSON {key} is not a string")
    return item


def _optional_bool(value: dict[str, object], key: str) -> bool | None:
    item = value[key]
    if item is not None and not isinstance(item, bool):
        raise ValueError(f"conversation JSON {key} is not a boolean")
    return item


def _json_int(value: dict[str, object], key: str) -> int:
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, (int, str)):
        raise ValueError(f"conversation JSON {key} is not an integer")
    return int(item)


def _optional_json_int(value: dict[str, object], key: str) -> int | None:
    item = value[key]
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, (int, str)):
        raise ValueError(f"conversation JSON {key} is not an integer")
    return int(item)


@dataclass
class _PayloadPlan:
    reference: conversation_projection.PayloadRef
    prefix_chunks: int
    prefix_bytes: int
    fragments: list[str]
    replaced: bool


async def _write_conversation_payloads(
    session: AsyncSession, thread_id: UUID, writes: Sequence[conversation_projection.PayloadWrite]
) -> None:
    plans_by_reference: dict[conversation_projection.PayloadRef, _PayloadPlan] = {}
    final_plans: dict[tuple[int, str, conversation_projection.PayloadField], _PayloadPlan] = {}
    for write in writes:
        if isinstance(write, conversation_projection.ReplacePayload):
            plan = _PayloadPlan(write.reference, 0, 0, [write.text], True)
        else:
            prior = plans_by_reference.get(write.base) if write.base is not None else None
            if prior is None:
                if write.base is None:
                    prior_chunks = 0
                    prior_bytes = 0
                else:
                    manifest = await session.get(
                        ConversationPayloadManifest, _payload_manifest_key(thread_id, write.base)
                    )
                    if manifest is None:
                        raise ValueError("append references a missing payload manifest")
                    prior_chunks = manifest.chunk_count
                    prior_bytes = manifest.content_bytes
                plan = _PayloadPlan(write.reference, prior_chunks, prior_bytes, [write.text], False)
            else:
                prior.fragments.append(write.text)
                plan = _PayloadPlan(
                    write.reference, prior.prefix_chunks, prior.prefix_bytes, prior.fragments, prior.replaced
                )
        plans_by_reference[plan.reference] = plan
        final_plans[(plan.reference.owner_cursor, plan.reference.owner_item_id, plan.reference.field)] = plan
    for plan in final_plans.values():
        text = "".join(plan.fragments)
        text_bytes = len(text.encode())
        chunk_count = plan.prefix_chunks if not text else plan.prefix_chunks + 1
        await session.execute(
            insert(ConversationPayloadManifest).values(
                thread_id=thread_id,
                source_id=plan.reference.source_id,
                projection_epoch=plan.reference.projection_epoch,
                owner_cursor=plan.reference.owner_cursor,
                owner_id=plan.reference.owner_item_id,
                field=plan.reference.field,
                generation=plan.reference.generation,
                revision_cursor=plan.reference.revision_cursor,
                present=True,
                chunk_count=chunk_count,
                content_bytes=text_bytes if plan.replaced else plan.prefix_bytes + text_bytes,
            )
        )
        if chunk_count > plan.prefix_chunks:
            session.add(
                ConversationPayloadChunk(
                    thread_id=thread_id,
                    source_id=plan.reference.source_id,
                    projection_epoch=plan.reference.projection_epoch,
                    owner_cursor=plan.reference.owner_cursor,
                    owner_id=plan.reference.owner_item_id,
                    field=plan.reference.field,
                    generation=plan.reference.generation,
                    chunk_index=plan.prefix_chunks,
                    text=text,
                )
            )


def _payload_manifest_key(
    thread_id: UUID, reference: conversation_projection.PayloadRef
) -> tuple[UUID, str, str, int, str, str, int, int]:
    return (
        thread_id,
        reference.source_id,
        reference.projection_epoch,
        reference.owner_cursor,
        reference.owner_item_id,
        reference.field,
        reference.generation,
        reference.revision_cursor,
    )


def _conversation_entities(thread_id: UUID, result: conversation_projection.ProjectionBatch) -> list[dict[str, object]]:
    entities = [_view_state_entity(thread_id, result.state)]
    entities.extend(_item_entity(thread_id, item) for item in result.item_upserts)
    entities.extend(_confirmed_input_entity(thread_id, value) for value in result.confirmed_input_upserts)
    entities.extend(_lifecycle_entity(thread_id, value) for value in result.lifecycle_upserts)
    entities.extend(_command_entity(thread_id, value) for value in result.command_upserts)
    return entities


def _entity_values(
    thread_id: UUID,
    source_id: str,
    projection_epoch: str,
    entity_kind: str,
    entity_id: str,
    cursor: int,
    revision_cursor: int,
    state: dict[str, object],
    *,
    pending: bool = False,
    turn_id: str | None = None,
    text_ref: conversation_projection.FieldValue | None = None,
    arguments_ref: conversation_projection.FieldValue | None = None,
    output_ref: conversation_projection.FieldValue | None = None,
    input_ref: conversation_projection.FieldValue | None = None,
) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "source_id": source_id,
        "projection_epoch": projection_epoch,
        "entity_kind": entity_kind,
        "entity_id": entity_id,
        "cursor": cursor,
        "revision_cursor": revision_cursor,
        "pending": pending,
        "turn_id": turn_id,
        "state": state,
        "text_ref": _payload_ref_json(text_ref),
        "arguments_ref": _payload_ref_json(arguments_ref),
        "output_ref": _payload_ref_json(output_ref),
        "input_ref": _payload_ref_json(input_ref),
    }


def _view_state_entity(thread_id: UUID, state: conversation_projection.ViewState) -> dict[str, object]:
    return _entity_values(
        thread_id,
        state.position.source_id,
        state.position.projection_epoch,
        "view_state",
        "current",
        state.position.through_cursor,
        state.position.through_cursor,
        {
            "controls": {
                "applied_model": state.controls.applied_model,
                "active_turn_id": state.controls.active_turn_id,
                "harness_state": state.controls.harness_state,
            },
            "unresolved_count": state.unresolved_count,
        },
    )


def _item_entity(thread_id: UUID, item: conversation_projection.ConversationItem) -> dict[str, object]:
    return _entity_values(
        thread_id,
        item.source_id,
        item.projection_epoch,
        "item",
        item.item_id,
        item.cursor,
        item.revision_cursor,
        {
            "kind": item.kind,
            "tool_name": item.tool_name,
            "completion": item.completion,
            "tool_succeeded": item.tool_succeeded,
        },
        turn_id=item.turn_id,
        text_ref=item.text,
        arguments_ref=item.arguments,
        output_ref=item.output,
    )


def _confirmed_input_entity(thread_id: UUID, value: conversation_projection.ConfirmedInput) -> dict[str, object]:
    return _entity_values(
        thread_id,
        value.source_id,
        value.projection_epoch,
        "confirmed_input",
        str(value.cursor),
        value.cursor,
        value.revision_cursor,
        {"harness_message_id": value.harness_message_id, "origin_command_ids": list(value.origin_command_ids)},
        turn_id=value.turn_id,
        input_ref=value.text,
    )


def _lifecycle_entity(thread_id: UUID, value: conversation_projection.LifecycleSegment) -> dict[str, object]:
    return _entity_values(
        thread_id,
        value.source_id,
        value.projection_epoch,
        "lifecycle",
        str(value.cursor),
        value.cursor,
        value.revision_cursor,
        {"observation": value.observation, "event": MessageToDict(value.event)},
    )


def _command_entity(thread_id: UUID, value: conversation_projection.CommandSummary) -> dict[str, object]:
    return _entity_values(
        thread_id,
        value.source_id,
        value.projection_epoch,
        "command",
        value.command_id,
        value.admission_cursor,
        value.outcome_cursor or value.admission_cursor,
        {
            "operation": value.operation,
            "outcome": value.outcome,
            "outcome_cursor": str(value.outcome_cursor) if value.outcome_cursor is not None else None,
            "outcome_reason": value.outcome_reason,
        },
        pending=value.outcome is conversation_projection.CommandOutcome.PENDING,
        input_ref=value.input,
    )


def _payload_ref_json(value: conversation_projection.FieldValue | None) -> dict[str, str] | None:
    if value is None:
        return None
    reference = value.reference
    return {
        "source_id": reference.source_id,
        "projection_epoch": reference.projection_epoch,
        "owner_cursor": str(reference.owner_cursor),
        "owner_item_id": reference.owner_item_id,
        "field": reference.field,
        "revision_cursor": str(reference.revision_cursor),
        "generation": str(reference.generation),
    }


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


async def _last(session: AsyncSession, thread_id: UUID) -> tuple[int | None, datetime | None, dict[str, object] | None]:
    last_cursor = await session.scalar(
        select(Event.cursor).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
    )
    last_at = await session.scalar(
        select(Event.at).where(Event.thread_id == thread_id).order_by(Event.at.desc()).limit(1)
    )
    state = await session.get(FeedState, thread_id)
    return last_cursor, last_at, (state.attached if state is not None else None)


def _view(
    thread: Thread, last_cursor: int | None, last_at: datetime | None, attached: dict[str, object] | None
) -> ThreadView:
    harness_state = (
        ParseDict(attached, protocol_pb2.Attached()).harness_state
        if attached is not None
        else protocol_pb2.HARNESS_STATE_UNSPECIFIED
    )
    return ThreadView(
        id=thread.id,
        sandbox=thread.sandbox,
        session_id=thread.session_id,
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
