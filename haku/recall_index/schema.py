"""SQLAlchemy schema for the Haku semantic index.

The semantic index has three layers:

- ``contents`` is the global, content-addressed collection of exact strings the document
  embedder sees.  ``content_sha`` always means the SHA-256 of ``content.encode("utf-8")``.
- ``content_embeddings`` is the vector produced when one such string is embedded by one model.
  It is durable index data, not an evictable cache: a model migration adds rows here while
  retaining the input content.
- index-type tables describe occurrences of that content. Git chunk occurrences identify a span in
  a blob; chat windows identify a span in a conversation and the messages they cover.

Keeping content identity separate from occurrences lets identical input text share a vector across
Git and conversations, across revisions, and across chunker layouts. Git and chat rows hold the
provenance a result needs to cite; only the content and its embedding are global. Every occurrence
belongs to a durable logical index: callers will eventually be granted an index, never an unscoped
collection of occurrences.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haku.recall_index.vector_type import HalfVector

SCHEMA = "recall_index"


class IndexType(StrEnum):
    """The storage and provenance shape of one logical index."""

    GIT = "git"
    CHAT = "chat"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class RecallIndex(Base):
    """One logical recall boundary, independent of the content-addressed embedding cache."""

    __tablename__ = "indexes"
    __table_args__ = (CheckConstraint("index_type IN ('git', 'chat')", name="ck_indexes_index_type"),)

    index_id: Mapped[str] = mapped_column(Text, primary_key=True)
    index_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Content(Base):
    """One exact normalized input string, globally content-addressed.

    The stored value is deliberately named ``content`` rather than ``text`` or ``plaintext``:
    it is the canonical content whose hash names it and whose bytes are sent to an embedder.
    """

    __tablename__ = "contents"

    content_sha: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ContentEmbedding(Base):
    """One model's semantic representation of one globally-addressed content value."""

    __tablename__ = "content_embeddings"

    content_sha: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.contents.content_sha"), primary_key=True)
    # The model key identifies the vector space.  It is part of the key because the same content
    # may be embedded by a replacement model or a distinct document-normalization regime.
    model_key: Mapped[str] = mapped_column(Text, primary_key=True)
    # Unconstrained ``halfvec``: the dimension belongs to ``model_key``.  See vector_type.py for
    # why the index uses half precision and why a dimension typmod would make model changes DDL.
    embedding: Mapped[list[float]] = mapped_column(HalfVector, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GitChunk(Base):
    """A chunk occurrence in one index's Git blob under one chunker regime."""

    __tablename__ = "git_chunks"

    index_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.indexes.index_id"), primary_key=True)
    blob_sha: Mapped[str] = mapped_column(Text, primary_key=True)
    chunker_key: Mapped[str] = mapped_column(Text, primary_key=True)
    byte_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    byte_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.contents.content_sha"), nullable=False)


class GitTipEntry(Base):
    """One path at one index's indexed commit. Replaced wholesale every sync."""

    __tablename__ = "git_tip"

    index_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.indexes.index_id"), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    blob_sha: Mapped[str] = mapped_column(Text, nullable=False)


class GitSyncState(Base):
    """What one index's Git branch holds and what its ``git_tip`` holds."""

    __tablename__ = "git_sync_state"
    __table_args__ = (
        CheckConstraint(
            "(commit_sha IS NULL) = (chunker_key IS NULL) AND (commit_sha IS NULL) = (synced_at IS NULL)",
            name="ck_git_sync_state_indexed_half",
        ),
    )

    index_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.indexes.index_id"), primary_key=True)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    remote_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_seen_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunker_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatChunk(Base):
    """One index's searchable window of a chat session, pointing to global content."""

    __tablename__ = "chat_chunks"

    index_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.indexes.index_id"), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    window_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The thread the window's session ran, copied from the console at materialization. Search joins
    # it to the console's `conversation` row and its pinned `access_profile_id` — the occurrence
    # links to the conversation and never duplicates a profile label. By value like `session_id`:
    # this schema deliberately holds no foreign key into the console's tables.
    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    content_sha: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.contents.content_sha"), nullable=False)
    first_message_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_message_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChatChunkMessage(Base):
    """This chat window holds these messages, in order."""

    __tablename__ = "chat_chunk_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["index_id", "session_id", "window_no"],
            [f"{SCHEMA}.chat_chunks.index_id", f"{SCHEMA}.chat_chunks.session_id", f"{SCHEMA}.chat_chunks.window_no"],
            ondelete="CASCADE",
        ),
        Index("idx_chat_chunk_messages_message", "message_id"),
    )

    index_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    window_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)


class ChatSessionState(Base):
    """The source shape and chunker regime at which one session was last materialized."""

    __tablename__ = "chat_sessions"

    index_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.indexes.index_id"), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_message_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chunker_key: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
