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
from typing import Literal, Self
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
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
from agentplane.app.conversation_debug import (
    ArchivedObservation,
    ConversationEvidenceNotFoundError,
    ConversationScopeChangedError,
    EvidenceObservation,
    EvidencePage,
    NativeFrame,
    NativeFramePage,
    ObservationPage,
)
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
            "ix_conversation_entity_pending_command_cursor",
            "thread_id",
            "source_id",
            "projection_epoch",
            "cursor",
            "entity_id",
            postgresql_where=text("pending AND entity_kind = 'command'"),
        ),
        Index(
            "ix_conversation_entity_scope_segment_cursor",
            "thread_id",
            "source_id",
            "projection_epoch",
            "cursor",
            postgresql_where=text("entity_kind IN ('item', 'confirmed_input', 'lifecycle')"),
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


class ConversationInterestExpiredError(ValueError):
    """A bounded browser interest must be resolved again at the current projection position."""


CommandOutcomeValue = Literal["pending", "effected", "failed", "noop"]


class ConversationScopeResetError(ValueError):
    """A browser's retained projection source or epoch is no longer current."""


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


@dataclass(frozen=True)
class ConversationEntityInterest:
    scope: ConversationScope
    anchor_cursor: int
    tail_from: int
    window_from: int | None = None
    window_before: int | None = None


@dataclass(frozen=True)
class ConversationPendingInterest:
    """One server-selected command page and the view-state revision that selected it."""

    scope: ConversationScope
    command_revision_cursor: int
    unresolved_count: int
    command_ids: tuple[str, ...]
    next_before_cursor: int | None


@dataclass(frozen=True)
class ConversationPayloadSelection:
    scope: ConversationScope
    owner_cursor: int
    owner_id: str
    field: str
    generation: int
    revision_cursor: int
    present: bool
    chunk_count: int
    content_bytes: int


class ConversationPayloadReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    projection_epoch: str
    owner_cursor: str
    owner_item_id: str
    field: Literal["text", "arguments", "output", "confirmed_input", "command_input"]
    revision_cursor: str
    generation: str


class ConversationControlsState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_model: str | None
    active_turn_id: str | None
    harness_state: str | None


class ConversationFeedErrorState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str
    message: str


class ConversationOperationalState(BaseModel):
    """Feed lifecycle state with an independent version, never a fabricated runner cursor."""

    model_config = ConfigDict(extra="forbid")

    operational_version: str
    status: Literal["active", "ended", "failed"]
    last_verified_cursor: str
    feed_error: ConversationFeedErrorState | None


class ConversationViewState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    controls: ConversationControlsState
    unresolved_count: int
    command_revision_cursor: str
    operational: ConversationOperationalState


class ConversationItemState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: int
    tool_name: str
    completion: str | None
    tool_succeeded: bool | None


class ConversationConfirmedInputState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harness_message_id: str
    origin_command_ids: list[str]


class ConversationLifecycleState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: str
    event: JsonValue


class ConversationCommandState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    outcome: CommandOutcomeValue
    outcome_cursor: str | None
    outcome_reason: str | None


class ConversationStoredEntity(BaseModel):
    """The generated client contract for a synchronized current conversation row."""

    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    source_id: str
    projection_epoch: str
    entity_kind: Literal["view_state", "item", "confirmed_input", "lifecycle", "command"]
    entity_id: str
    cursor: int
    revision_cursor: int
    pending: bool
    turn_id: str | None
    state: (
        ConversationViewState
        | ConversationItemState
        | ConversationConfirmedInputState
        | ConversationLifecycleState
        | ConversationCommandState
    )
    text_ref: ConversationPayloadReference | None
    arguments_ref: ConversationPayloadReference | None
    output_ref: ConversationPayloadReference | None
    input_ref: ConversationPayloadReference | None

    @model_validator(mode="after")
    def _state_matches_kind(self) -> Self:
        expected = {
            "view_state": ConversationViewState,
            "item": ConversationItemState,
            "confirmed_input": ConversationConfirmedInputState,
            "lifecycle": ConversationLifecycleState,
            "command": ConversationCommandState,
        }[self.entity_kind]
        if not isinstance(self.state, expected):
            raise ValueError(f"conversation state does not match {self.entity_kind}")
        return self


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

    async def conversation_entity_interest(
        self,
        thread_id: UUID,
        *,
        anchor_cursor: int | None = None,
        before_cursor: int | None = None,
        page_size: int = 30,
    ) -> ConversationEntityInterest | None:
        if not 1 <= page_size <= 100:
            raise ValueError("conversation page size must be between 1 and 100")
        segment_kinds = ("item", "confirmed_input", "lifecycle")
        async with self._sessions() as session:
            checkpoint = await session.scalar(
                select(ConversationProjectionCheckpoint).where(ConversationProjectionCheckpoint.thread_id == thread_id)
            )
            if checkpoint is None:
                return None
            scope = ConversationScope(checkpoint.source_id, checkpoint.projection_epoch, checkpoint.through_cursor)
            anchor = scope.through_cursor if anchor_cursor is None else anchor_cursor
            if anchor < 0 or anchor > scope.through_cursor:
                raise ValueError("conversation anchor is outside the projected prefix")
            common = (
                ConversationEntity.thread_id == thread_id,
                ConversationEntity.source_id == scope.source_id,
                ConversationEntity.projection_epoch == scope.projection_epoch,
                ConversationEntity.entity_kind.in_(segment_kinds),
            )
            if anchor_cursor is not None:
                newer = list(
                    await session.scalars(
                        select(ConversationEntity.cursor)
                        .where(*common, ConversationEntity.cursor > anchor)
                        .order_by(ConversationEntity.cursor)
                        .limit(page_size * 2 + 1)
                    )
                )
                if len(newer) > page_size * 2:
                    raise ConversationInterestExpiredError("conversation interest must rotate")

            async def lower(before: int) -> int:
                cursors = list(
                    await session.scalars(
                        select(ConversationEntity.cursor)
                        .where(*common, ConversationEntity.cursor < before)
                        .order_by(ConversationEntity.cursor.desc())
                        .limit(page_size)
                    )
                )
                return cursors[-1] if cursors else before

            tail_from = await lower(anchor + 1)
            if before_cursor is None:
                return ConversationEntityInterest(scope, anchor, tail_from)
            if before_cursor < 0:
                raise ValueError("before cursor cannot be negative")
            return ConversationEntityInterest(scope, anchor, tail_from, await lower(before_cursor), before_cursor)

    async def pending_command_interest(
        self, thread_id: UUID, *, before_cursor: int | None = None, page_size: int = 30
    ) -> ConversationPendingInterest | None:
        """Return a cursor-keyset page of unresolved commands from one read-only snapshot."""
        if not 1 <= page_size <= 100:
            raise ValueError("pending command page size must be between 1 and 100")
        if before_cursor is not None and before_cursor < 0:
            raise ValueError("before cursor cannot be negative")
        async with self._sessions.begin() as session:
            # PostgreSQL otherwise gives every statement in this method its own READ COMMITTED
            # snapshot, which could pair an old command page with a newer view-state revision.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            checkpoint = await session.get(ConversationProjectionCheckpoint, thread_id)
            if checkpoint is None:
                return None
            scope = ConversationScope(checkpoint.source_id, checkpoint.projection_epoch, checkpoint.through_cursor)
            view_row = await session.get(
                ConversationEntity, (thread_id, scope.source_id, scope.projection_epoch, "view_state", "current")
            )
            if view_row is None:
                raise ValueError("conversation checkpoint has no current controls")
            view = ConversationViewState.model_validate(view_row.state)
            before = scope.through_cursor + 1 if before_cursor is None else before_cursor
            rows = list(
                await session.execute(
                    select(ConversationEntity.cursor, ConversationEntity.entity_id)
                    .where(
                        ConversationEntity.thread_id == thread_id,
                        ConversationEntity.source_id == scope.source_id,
                        ConversationEntity.projection_epoch == scope.projection_epoch,
                        ConversationEntity.entity_kind == "command",
                        ConversationEntity.pending,
                        ConversationEntity.cursor < before,
                    )
                    .order_by(ConversationEntity.cursor.desc())
                    .limit(page_size + 1)
                )
            )
            has_older = len(rows) > page_size
            page = rows[:page_size]
            return ConversationPendingInterest(
                scope,
                int(view.command_revision_cursor),
                view.unresolved_count,
                tuple(row.entity_id for row in page),
                page[-1].cursor if has_older and page else None,
            )

    async def conversation_payload_selection(
        self, thread_id: UUID, *, owner_cursor: int, owner_id: str, field: str, generation: int, revision_cursor: int
    ) -> ConversationPayloadSelection | None:
        async with self._sessions() as session:
            checkpoint = await session.scalar(
                select(ConversationProjectionCheckpoint).where(ConversationProjectionCheckpoint.thread_id == thread_id)
            )
            if checkpoint is None:
                return None
            manifest = await session.scalar(
                select(ConversationPayloadManifest).where(
                    ConversationPayloadManifest.thread_id == thread_id,
                    ConversationPayloadManifest.source_id == checkpoint.source_id,
                    ConversationPayloadManifest.projection_epoch == checkpoint.projection_epoch,
                    ConversationPayloadManifest.owner_cursor == owner_cursor,
                    ConversationPayloadManifest.owner_id == owner_id,
                    ConversationPayloadManifest.field == field,
                    ConversationPayloadManifest.generation == generation,
                    ConversationPayloadManifest.revision_cursor == revision_cursor,
                )
            )
            if manifest is None:
                return None
            return ConversationPayloadSelection(
                ConversationScope(manifest.source_id, manifest.projection_epoch, checkpoint.through_cursor),
                owner_cursor,
                owner_id,
                field,
                generation,
                revision_cursor,
                manifest.present,
                manifest.chunk_count,
                manifest.content_bytes,
            )

    async def conversation_evidence(
        self,
        thread_id: UUID,
        *,
        source_id: str,
        projection_epoch: str,
        entity_kind: str,
        entity_id: str,
        after_cursor: int,
        limit: int,
    ) -> EvidencePage:
        if not 1 <= limit <= 200 or after_cursor < 0:
            raise ValueError("invalid evidence page bounds")
        async with self._sessions() as session:
            entity_cursor = await _evidence_entity_cursor(
                session, thread_id, source_id, projection_epoch, entity_kind, entity_id
            )
            native_exists = (
                select(ConversationProjectionNativeLink.source_sequence)
                .where(
                    ConversationProjectionNativeLink.thread_id == ConversationProjectionEvidence.thread_id,
                    ConversationProjectionNativeLink.source_id == ConversationProjectionEvidence.source_id,
                    ConversationProjectionNativeLink.projection_epoch
                    == ConversationProjectionEvidence.projection_epoch,
                    ConversationProjectionNativeLink.entity_cursor == ConversationProjectionEvidence.entity_cursor,
                    ConversationProjectionNativeLink.observation_cursor
                    == ConversationProjectionEvidence.observation_cursor,
                )
                .exists()
            )
            rows = list(
                await session.execute(
                    select(ConversationProjectionEvidence.observation_cursor, native_exists)
                    .where(
                        ConversationProjectionEvidence.thread_id == thread_id,
                        ConversationProjectionEvidence.source_id == source_id,
                        ConversationProjectionEvidence.projection_epoch == projection_epoch,
                        ConversationProjectionEvidence.entity_cursor == entity_cursor,
                        ConversationProjectionEvidence.observation_cursor > after_cursor,
                    )
                    .order_by(ConversationProjectionEvidence.observation_cursor)
                    .limit(limit + 1)
                )
            )
            return EvidencePage(
                observations=[
                    EvidenceObservation(observation_cursor=str(cursor), has_native=native)
                    for cursor, native in rows[:limit]
                ],
                next_after_cursor=str(rows[limit - 1][0]) if len(rows) > limit else None,
            )

    async def conversation_native_frames(
        self,
        thread_id: UUID,
        *,
        source_id: str,
        projection_epoch: str,
        entity_kind: str,
        entity_id: str,
        observation_cursor: int,
        after_sequence: int,
        limit: int,
    ) -> NativeFramePage:
        if not 1 <= limit <= 200 or after_sequence < 0:
            raise ValueError("invalid native frame page bounds")
        async with self._sessions() as session:
            entity_cursor = await _evidence_entity_cursor(
                session, thread_id, source_id, projection_epoch, entity_kind, entity_id
            )
            association = await session.get(
                ConversationProjectionEvidence,
                (thread_id, source_id, projection_epoch, entity_cursor, observation_cursor),
            )
            if association is None:
                raise ConversationEvidenceNotFoundError("no evidence association for the selected observation")
            rows = list(
                await session.execute(
                    select(ConversationProjectionNativeLink.source_sequence, Event.payload)
                    .outerjoin(
                        Event,
                        (
                            (Event.thread_id == ConversationProjectionNativeLink.thread_id)
                            & (Event.origin_source_id == ConversationProjectionNativeLink.source_id)
                            & (Event.origin_sequence == ConversationProjectionNativeLink.source_sequence)
                            & (Event.kind == "native")
                        ),
                    )
                    .where(
                        ConversationProjectionNativeLink.thread_id == thread_id,
                        ConversationProjectionNativeLink.source_id == source_id,
                        ConversationProjectionNativeLink.projection_epoch == projection_epoch,
                        ConversationProjectionNativeLink.entity_cursor == entity_cursor,
                        ConversationProjectionNativeLink.observation_cursor == observation_cursor,
                        ConversationProjectionNativeLink.source_sequence > after_sequence,
                    )
                    .order_by(ConversationProjectionNativeLink.source_sequence)
                    .limit(limit + 1)
                )
            )
            return NativeFramePage(
                frames=[
                    NativeFrame.model_validate(
                        {
                            "source_sequence": str(sequence),
                            "availability": "present" if payload is not None else "unavailable",
                            "entry": payload,
                        }
                    )
                    for sequence, payload in rows[:limit]
                ],
                next_after_sequence=str(rows[limit - 1][0]) if len(rows) > limit else None,
            )

    async def conversation_observations(
        self, thread_id: UUID, *, before_cursor: int | None = None, after_cursor: int | None = None, limit: int = 30
    ) -> ObservationPage:
        """Seek directly into the immutable archive; never fold or load intervening history."""
        if (
            not 1 <= limit <= 200
            or (before_cursor is not None and before_cursor < 0)
            or (after_cursor is not None and after_cursor < 0)
            or (before_cursor is not None and after_cursor is not None)
        ):
            raise ValueError("invalid chronological observation page bounds")
        async with self._sessions() as session:
            query = select(Event).where(Event.thread_id == thread_id)
            if after_cursor is not None:
                query = query.where(Event.cursor > after_cursor).order_by(Event.cursor)
            else:
                if before_cursor is not None:
                    query = query.where(Event.cursor < before_cursor)
                query = query.order_by(Event.cursor.desc())
            rows = list(await session.scalars(query.limit(limit)))
            if after_cursor is None:
                rows.reverse()
            if not rows:
                return ObservationPage(observations=[], next_before_cursor=None, next_after_cursor=None)
            has_older = await session.scalar(
                select(select(Event.cursor).where(Event.thread_id == thread_id, Event.cursor < rows[0].cursor).exists())
            )
            has_newer = await session.scalar(
                select(
                    select(Event.cursor).where(Event.thread_id == thread_id, Event.cursor > rows[-1].cursor).exists()
                )
            )
            return ObservationPage(
                observations=[
                    ArchivedObservation.model_validate(
                        {
                            "cursor": str(row.cursor),
                            "source_id": row.origin_source_id,
                            "source_sequence": str(row.origin_sequence),
                            "kind": row.kind,
                            "entry": row.payload,
                        }
                    )
                    for row in rows
                ],
                next_before_cursor=str(rows[0].cursor) if has_older else None,
                next_after_cursor=str(rows[-1].cursor) if has_newer else None,
            )

    async def command_outcomes(
        self, thread_id: UUID, source_id: str, projection_epoch: str, command_ids: Sequence[str]
    ) -> dict[str, CommandOutcomeValue | None]:
        """Current outcomes for a finite browser-held command-id set, keyed by entity primary key."""
        requested = tuple(dict.fromkeys(command_ids))
        async with self._sessions() as session:
            if await session.get(Thread, thread_id) is None:
                raise ThreadNotFoundError(thread_id)
            checkpoint = await session.get(ConversationProjectionCheckpoint, thread_id)
            if checkpoint is None or (checkpoint.source_id, checkpoint.projection_epoch) != (
                source_id,
                projection_epoch,
            ):
                raise ConversationScopeResetError("conversation projection scope was reset")
            rows = await session.scalars(
                select(ConversationEntity).where(
                    ConversationEntity.thread_id == thread_id,
                    ConversationEntity.source_id == source_id,
                    ConversationEntity.projection_epoch == projection_epoch,
                    ConversationEntity.entity_kind == "command",
                    ConversationEntity.entity_id.in_(requested),
                )
            )
            outcomes = {row.entity_id: ConversationCommandState.model_validate(row.state).outcome for row in rows}
            return {command_id: outcomes.get(command_id) for command_id in requested}

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
            await _set_conversation_operational(session, thread_id, status="active", error=None)
            await _notify(session)

    async def end_feed(
        self, thread_id: UUID, *, lease: IngestionLease, error: str | None, error_cursor: int | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            state = await session.get(FeedState, thread_id)
            if state is None:
                raise ValueError("cannot end a feed before persisting its attachment")
            state.end = {} if error is None else {"message": error}
            await _set_conversation_operational(
                session,
                thread_id,
                status="ended" if error is None else "failed",
                error=error,
                error_cursor=error_cursor,
            )
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
            checkpoint = await session.get(ConversationProjectionCheckpoint, thread_id)
            if checkpoint is None:
                return None
            summary = await session.get(
                ConversationEntity,
                (thread_id, checkpoint.source_id, checkpoint.projection_epoch, "command", command.command_id),
            )
            if summary is None:
                return None
            payload = await session.scalar(
                select(Event.payload).where(Event.thread_id == thread_id, Event.cursor == summary.cursor)
            )
            if payload is None:
                raise ValueError("command summary has no archived admission")
            entry = ParseDict(payload, event_log_pb2.EventEntry())
            if (
                not entry.event.HasField("command_admitted")
                or entry.event.command_admitted.command.command_id != command.command_id
            ):
                raise ValueError("command summary does not point to its archived admission")
            admitted = entry.event.command_admitted.command
            if admitted == command:
                return entry
            raise CommandIdConflictError(f"command id {command.command_id!r} was already admitted with different work")

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
        operational = None
    else:
        if checkpoint.source_id != source_id:
            raise EventReplicationError(f"conversation source changed at cursor {entries[0].cursor}")
        if checkpoint.projection_epoch != CONVERSATION_PROJECTION_EPOCH:
            raise ConversationProjectionError(
                f"conversation projection epoch {checkpoint.projection_epoch!r} must be reset for "
                f"{CONVERSATION_PROJECTION_EPOCH!r}"
            )
        state, operational = await _conversation_state(session, checkpoint)
    batch = conversation_projection.EventBatch(source_id, state.position.through_cursor, tuple(entries))
    result = conversation_projection.advance(
        state, batch, await _prior_conversation_entities(session, thread_id, batch)
    )
    await _write_conversation_payloads(session, thread_id, result.payload_writes)
    for values in _conversation_entities(thread_id, result, operational):
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
) -> tuple[conversation_projection.ViewState, ConversationOperationalState]:
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
    view = ConversationViewState.model_validate(row.state)
    return conversation_projection.ViewState(
        conversation_projection.Position(checkpoint.source_id, checkpoint.projection_epoch, checkpoint.through_cursor),
        conversation_projection.Controls(
            applied_model=view.controls.applied_model,
            active_turn_id=view.controls.active_turn_id,
            harness_state=view.controls.harness_state,
        ),
        view.unresolved_count,
        int(view.command_revision_cursor),
    ), view.operational


async def _set_conversation_operational(
    session: AsyncSession,
    thread_id: UUID,
    *,
    status: Literal["active", "ended", "failed"],
    error: str | None,
    error_cursor: int | None = None,
) -> None:
    checkpoint = await session.get(ConversationProjectionCheckpoint, thread_id)
    if checkpoint is None:
        return
    row = await session.get(
        ConversationEntity, (thread_id, checkpoint.source_id, checkpoint.projection_epoch, "view_state", "current")
    )
    if row is None:
        raise ValueError("conversation checkpoint has no current controls")
    view = ConversationViewState.model_validate(row.state)
    row.state = view.model_copy(
        update={
            "operational": ConversationOperationalState(
                operational_version=str(int(view.operational.operational_version) + 1),
                status=status,
                last_verified_cursor=str(checkpoint.through_cursor),
                feed_error=(
                    None
                    if error is None
                    else ConversationFeedErrorState(
                        cursor=str(error_cursor if error_cursor is not None else checkpoint.through_cursor),
                        message=error,
                    )
                ),
            )
        }
    ).model_dump(mode="json")


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


def _conversation_entities(
    thread_id: UUID,
    result: conversation_projection.ProjectionBatch,
    operational: ConversationOperationalState | None = None,
) -> list[dict[str, object]]:
    entities = [_view_state_entity(thread_id, result.state, operational)]
    entities.extend(_item_entity(thread_id, item) for item in result.item_upserts)
    entities.extend(_confirmed_input_entity(thread_id, value) for value in result.confirmed_input_upserts)
    entities.extend(_lifecycle_entity(thread_id, value) for value in result.lifecycle_upserts)
    entities.extend(_command_entity(thread_id, value) for value in result.command_upserts)
    return entities


def _entity_values(
    thread_id: UUID,
    source_id: str,
    projection_epoch: str,
    entity_kind: Literal["view_state", "item", "confirmed_input", "lifecycle", "command"],
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
    return ConversationStoredEntity(
        thread_id=thread_id,
        source_id=source_id,
        projection_epoch=projection_epoch,
        entity_kind=entity_kind,
        entity_id=entity_id,
        cursor=cursor,
        revision_cursor=revision_cursor,
        pending=pending,
        turn_id=turn_id,
        state=state,
        text_ref=_payload_ref_json(text_ref),
        arguments_ref=_payload_ref_json(arguments_ref),
        output_ref=_payload_ref_json(output_ref),
        input_ref=_payload_ref_json(input_ref),
    ).model_dump(mode="json")


def _view_state_entity(
    thread_id: UUID, state: conversation_projection.ViewState, operational: ConversationOperationalState | None = None
) -> dict[str, object]:
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
            "command_revision_cursor": str(state.command_revision_cursor),
            "operational": operational
            or ConversationOperationalState(
                operational_version="0",
                status="active",
                last_verified_cursor=str(state.position.through_cursor),
                feed_error=None,
            ),
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


async def _evidence_entity_cursor(
    session: AsyncSession, thread_id: UUID, source_id: str, projection_epoch: str, entity_kind: str, entity_id: str
) -> int:
    checkpoint = await session.get(ConversationProjectionCheckpoint, thread_id)
    if checkpoint is None:
        raise ConversationEvidenceNotFoundError("no materialized conversation")
    if (checkpoint.source_id, checkpoint.projection_epoch) != (source_id, projection_epoch):
        raise ConversationScopeChangedError("the conversation source or projection epoch has changed")
    entity = await session.get(ConversationEntity, (thread_id, source_id, projection_epoch, entity_kind, entity_id))
    if entity is None:
        raise ConversationEvidenceNotFoundError("no selected conversation entity")
    return entity.cursor


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
