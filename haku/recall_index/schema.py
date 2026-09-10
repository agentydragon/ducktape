"""SQLAlchemy schema for the Haku semantic index.

The semantic index has three layers:

- ``contents`` is the global, content-addressed collection of exact strings the document
  embedder sees.  ``content_sha`` always means the SHA-256 of ``content.encode("utf-8")``.
- ``content_embeddings`` is the vector produced when one such string is embedded by one model.
  It is durable index data, not an evictable cache: a model migration adds rows here while
  retaining the input content.
- index-type tables describe occurrences of that content. Git chunk occurrences identify a span in
  a blob.

Keeping content identity separate from occurrences lets identical input text share a vector across
revisions and across chunker layouts. Git rows hold the provenance a result needs to cite; only the
content and its embedding are global. Every occurrence belongs to a durable logical index: callers
will eventually be granted an index, never an unscoped collection of occurrences.
"""

from __future__ import annotations

import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, MetaData, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haku.recall_index.vector_type import HalfVector

SCHEMA = "recall_index"


class IndexType(StrEnum):
    """The storage and provenance shape of one logical index."""

    GIT = "git"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class RecallIndex(Base):
    """One logical recall boundary, independent of the content-addressed embedding cache."""

    __tablename__ = "indexes"
    __table_args__ = (CheckConstraint("index_type IN ('git')", name="ck_indexes_index_type"),)

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
