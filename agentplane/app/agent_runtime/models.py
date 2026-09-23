"""The tables a thread is stored in.

The schema is owned by the Alembic migrations under `migrations/`, applied by `database_migrate.py`
as a separate deploy step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SqlEnum, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from agentplane.app.database import Base
from agentplane.app.presets import Harness


class EventLog(Base):
    """A runner session's Event sequence as the app copies it, minted on first sight.

    Everything recorded hangs off it; its id is the id the thread is known by.
    """

    __tablename__ = "event_log"
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
    # The spec's model, rewritten by ingestion as the feed reports a change.
    model: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Thread(Base):
    """What an operator has set on an event log's thread. No row means the defaults: unnamed and
    not archived, so ingestion never has to create one."""

    __tablename__ = "thread"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    # NULL while unnamed; never the empty string.
    name: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (Index("ix_event_thread_at", "thread_id", "at"),)

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    # `record` admits an entry only where `origin.sequence == cursor`, so this key is also the
    # runner's follow sequence that `ThreadNativeLink.source_sequence` names. The entry's own
    # proto-JSON `payload` names its `origin.source_id`, which is constant for a Thread.
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
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    attached: Mapped[dict[str, object]] = mapped_column(JSONB)
    # NULL means the stream has not ended. Empty JSON is a normal end; a message is an error end.
    end: Mapped[dict[str, str] | None] = mapped_column(JSONB(none_as_null=True))


class ThreadCheckpoint(Base):
    """One source/epoch-owned materialized prefix for a Thread.

    `thread_id` alone keys this row, and `ThreadStore.record` refuses an entry whose
    `origin.source_id` disagrees with the prefix, so this is the one place a Thread's runner
    source is stored: every other fold table is scoped by `(thread_id, projection_epoch)` and
    reads its source from here.
    """

    __tablename__ = "thread_checkpoint"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text)
    projection_epoch: Mapped[str] = mapped_column(Text)
    through_cursor: Mapped[int] = mapped_column(BigInteger)


class ThreadEntity(Base):
    """The mutable, tagged current row consumed by the thread view shape."""

    __tablename__ = "thread_entity"
    __table_args__ = (
        Index("ix_thread_entity_scope_revision", "thread_id", "projection_epoch", "revision_cursor"),
        Index("ix_thread_entity_scope_cursor", "thread_id", "projection_epoch", "cursor", "entity_kind", "entity_id"),
        Index(
            "ix_thread_entity_scope_pending_cursor",
            "thread_id",
            "projection_epoch",
            "cursor",
            "entity_kind",
            "entity_id",
            postgresql_where=text("pending"),
        ),
        Index(
            "ix_thread_entity_scope_segment_cursor",
            "thread_id",
            "projection_epoch",
            "cursor",
            postgresql_where=text("entity_kind IN ('item', 'confirmed_input', 'lifecycle')"),
        ),
        Index("ix_thread_entity_scope_entity_index", "thread_id", "projection_epoch", "entity_index", unique=True),
    )

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
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
    # A dense position in the thread, assigned once and never revised, over every kind rather than
    # only the rendered ones -- so a range of it is every row in that stretch of the thread,
    # whatever it is. A cursor cannot stand in: how many rows a cursor range covers
    # depends on how densely a turn packs them, so only an index gives fixed-size pages.
    entity_index: Mapped[int] = mapped_column(BigInteger)


class ThreadPayloadManifest(Base):
    """An immutable exact field revision; chunks are owned by its generation."""

    __tablename__ = "thread_payload_manifest"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chunk_count: Mapped[int] = mapped_column(BigInteger)
    content_bytes: Mapped[int] = mapped_column(BigInteger)


class ThreadPayloadChunk(Base):
    """A UTF-8 fragment, immutable within a payload generation."""

    __tablename__ = "thread_payload_chunk"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chunk_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class ThreadEvidence(Base):
    __tablename__ = "thread_evidence"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class ThreadNativeLink(Base):
    __tablename__ = "thread_native_link"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
