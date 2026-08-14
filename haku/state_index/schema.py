"""SQLAlchemy schema for the haku index.

`chunks` is the one table both corpora share: it is content-addressed, so it has no notion of
where the content currently lives and keeps embeddings for content that has left the indexed
set. That is the cache — a revert, a rebase, a re-windowed chat session all re-use vectors
instead of paying to recompute them.

Everything else is per-corpus, and each table says which corpus it belongs to:

- **git** — `git_tip` is the tree at the indexed commit, replaced wholesale each sync, and
  `git_sync_state` records what that commit was. Search joins `git_tip` to `chunks`, so **the
  join is the tip filter**: content no longer at the tip is unreachable by construction, not by
  a delete pass that could be missed.
- **chat** — `chat_chunks` is the searchable window set, `chat_chunk_messages` records which
  messages each window holds, and `chat_sessions` records the shape of each session as last
  indexed. Chat has no equivalent of the tip join: a session's rows are replaced when it grows,
  and a session that leaves the source is swept by the sync (`chat_sync.sync_chat`), so
  retraction there is a step someone has to keep running rather than a property of the query.

`Corpus` is in `chunks`' primary key, which is what keeps the two apart. Each corpus supplies
its own kind of content address and its own chunker, so `content_sha` and `chunker_version` are
only ever comparable within one corpus.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haku.state_index.vector_type import Vector
from util.sqlalchemy_types import TextBackedStrEnumColumn

SCHEMA = "state_index"


class Corpus(StrEnum):
    """Which body of content a chunk was embedded from.

    Part of `chunks`' primary key rather than a convention about hash lengths or key prefixes:
    a git blob sha and a hash of a rendered message window are different namespaces, and a
    search or a cache lookup that forgets to say which one it means is a bug the key shape
    should catch.
    """

    GIT = "git"
    CHAT = "chat"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Chunk(Base):
    """One embedded span of one piece of content, under one (corpus, chunker, model) regime.

    The primary key carries `chunker_version` and `model_key` so changing the chunker or the
    embedding model misses the cache instead of silently serving vectors computed over
    different text or by a different model. `chunker_version` is scoped by `corpus` — the two
    corpora chunk different things by different rules, and their version numbers move
    independently.
    """

    __tablename__ = "chunks"

    corpus: Mapped[Corpus] = mapped_column(TextBackedStrEnumColumn(Corpus), primary_key=True)
    # What this corpus addresses content by: the git blob sha for `git`, the sha256 of the
    # rendered message window for `chat`. Only ever compared within one corpus.
    content_sha: Mapped[str] = mapped_column(Text, primary_key=True)
    chunk_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunker_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(Text, primary_key=True)
    # Byte offsets of this chunk within the content `content_sha` addresses. For `git` that
    # content is the blob, so the span locates the chunk inside a file a caller can read back.
    # For `chat` the addressed content is the chunk itself, so the span covers all of it.
    byte_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Unconstrained `vector`: dimension is a property of `model_key`, and pinning a typmod here
    # would force a migration to change models. Nothing indexes this column (exact KNN at this
    # corpus size), and searches filter `corpus` + `model_key` in a materialized CTE before the
    # distance operator ever sees a row — pgvector errors on comparing different dimensions, so
    # that filter is load-bearing, not cosmetic.
    embedding: Mapped[list[float]] = mapped_column(Vector, nullable=False)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GitTipEntry(Base):
    """One path at the indexed commit. Replaced wholesale every sync."""

    __tablename__ = "git_tip"

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    blob_sha: Mapped[str] = mapped_column(Text, nullable=False)


class GitSyncState(Base):
    """What `git_tip` currently holds. One row; `id` is pinned to 1."""

    __tablename__ = "git_sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_git_sync_state_singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    chunker_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_key: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChatChunk(Base):
    """One searchable window of a chat session.

    Keyed by its position in the session rather than by its content, because two sessions can
    hold the same exchange verbatim: they are then two windows sharing one cached vector, and a
    search that matches it must be able to say which session each hit came from.
    """

    __tablename__ = "chat_chunks"

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    chunk_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_sha: Mapped[str] = mapped_column(Text, nullable=False)
    first_message_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_message_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChatChunkMessage(Base):
    """This chunk holds these messages, in this order.

    The pointer a hit hands back: a caller reads the real content through the console's own
    conversation tools (`haku/console/tools/conversations.py`) rather than trusting the copy in
    `chunks.text`, which is what the embedder saw and not necessarily what the row says now.
    """

    __tablename__ = "chat_chunk_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "chunk_no"],
            [f"{SCHEMA}.chat_chunks.session_id", f"{SCHEMA}.chat_chunks.chunk_no"],
            ondelete="CASCADE",
        ),
        # The reverse direction: which window holds a given message.
        Index("idx_chat_chunk_messages_message", "message_id"),
    )

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    chunk_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)


class ChatSessionState(Base):
    """The shape of one chat session as last indexed, which is what decides re-indexing.

    A session grows, so unlike a git tip there is no single commit to compare against. The
    message count and newest message time are that comparison: a session whose source still
    matches both, under the same regime, is skipped without reading its messages.
    """

    __tablename__ = "chat_sessions"

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_message_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chunker_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_key: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
