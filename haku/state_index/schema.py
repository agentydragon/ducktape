"""SQLAlchemy schema for the haku-state index.

Two tables, and the split between them is the whole design:

- `chunks` is content-addressed (`blob_sha`), so it has no notion of commits and keeps
  embeddings for content that has left the tip. That is the cache: a revert, a rebase, or a
  branch switch re-uses vectors instead of paying to recompute them.
- `tip` is the tree at the indexed commit, replaced wholesale each sync.

Search joins `tip` to `chunks`, so **the join is the tip filter** — content that is no longer
at the tip is unreachable by construction, not by a delete pass that could be missed.
"""

from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, MetaData, SmallInteger, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haku.state_index.vector_type import Vector

SCHEMA = "state_index"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Chunk(Base):
    """One embedded span of one blob, under one (chunker, model) regime.

    The primary key carries `chunker_version` and `model_key` so changing the chunker or the
    embedding model misses the cache instead of silently serving vectors computed over
    different text or by a different model.
    """

    __tablename__ = "chunks"

    blob_sha: Mapped[str] = mapped_column(Text, primary_key=True)
    chunk_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunker_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(Text, primary_key=True)
    byte_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Unconstrained `vector`: dimension is a property of `model_key`, and pinning a typmod here
    # would force a migration to change models. Nothing indexes this column (exact KNN at this
    # corpus size), and searches filter `model_key` in a materialized CTE before the distance
    # operator ever sees a row — pgvector errors on comparing different dimensions, so that
    # filter is load-bearing, not cosmetic.
    embedding: Mapped[list[float]] = mapped_column(Vector, nullable=False)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TipEntry(Base):
    """One path at the indexed commit. Replaced wholesale every sync."""

    __tablename__ = "tip"

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    blob_sha: Mapped[str] = mapped_column(Text, nullable=False)


class SyncState(Base):
    """What the `tip` table currently holds. One row; `id` is pinned to 1."""

    __tablename__ = "sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_sync_state_singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    chunker_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_key: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
