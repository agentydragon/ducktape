"""The thread fold's projection tables, keyed by public Session ID.

Each database holding a fold registers `Base.metadata` with its own migrations. The tables carry no
foreign key: what names a Session differs between the databases that hold a fold.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionProjectionLease(Base):
    """Replica ownership of projection work for one Session."""

    __tablename__ = "session_projection_lease"

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ThreadCheckpoint(Base):
    """One source/epoch-owned materialized prefix for a Thread.

    `thread_id` alone keys this row, and `ThreadStore.record` refuses an entry whose
    `origin.source_id` disagrees with the prefix, so this is the one place a Thread's runner
    source is stored: every other fold table is scoped by `(thread_id, projection_epoch)` and
    reads its source from here.
    """

    __tablename__ = "thread_checkpoint"

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text)
    projection_epoch: Mapped[str] = mapped_column(Text)
    through_cursor: Mapped[int] = mapped_column(BigInteger)


class ThreadHistorySummary(Base):
    """Event-derived sidebar metadata for service-backed Thread projection."""

    __tablename__ = "thread_history_summary"

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Preserve the protobuf enum number rather than a closed SQL enum.
    last_turn_status: Mapped[int | None] = mapped_column(BigInteger)
    attached: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    end: Mapped[dict[str, str] | None] = mapped_column(JSONB(none_as_null=True))
    # A confirmed resume invalidates EOF at this cursor until newer evidence arrives.
    resumed_after_cursor: Mapped[int | None] = mapped_column(BigInteger)
    # Inferred from model-originated events, not an observed provider request or cache receipt.
    last_model_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
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

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
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

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chunk_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # JSON string scalars preserve U+0000 as an escape while decoding back to a string in readers.
    text: Mapped[str] = mapped_column(JSON)


class ThreadEvidence(Base):
    __tablename__ = "thread_evidence"

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class ThreadNativeLink(Base):
    __tablename__ = "thread_native_link"

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    projection_epoch: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
