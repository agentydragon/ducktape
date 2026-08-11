"""Database operations over the haku-state index."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from haku.state_index.git_tree import TipEntry
from haku.state_index.schema import SCHEMA, Base, Chunk, SyncState, TipEntry as TipRow

# The filters must be applied before the distance operator sees a row: `chunks` may hold
# vectors of several dimensions (one per model regime) and pgvector errors when comparing
# across dimensions. MATERIALIZED pins that evaluation order rather than trusting the planner.
_SEARCH_SQL = text("""
    WITH candidates AS MATERIALIZED (
        SELECT t.path, c.chunk_no, c.byte_start, c.byte_end, c.text, c.embedding
        FROM state_index.tip t
        JOIN state_index.chunks c ON c.blob_sha = t.blob_sha
        WHERE c.chunker_version = :chunker_version
          AND c.model_key = :model_key
          AND (CAST(:path_prefix AS text) IS NULL OR starts_with(t.path, CAST(:path_prefix AS text)))
    )
    SELECT path, chunk_no, byte_start, byte_end, text,
           1 - (embedding <=> CAST(:query AS vector)) AS score
    FROM candidates
    ORDER BY embedding <=> CAST(:query AS vector)
    LIMIT :limit
""")


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    chunk_no: int
    byte_start: int
    byte_end: int
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class ChunkRow:
    """A chunk ready to be written: the cache key, the span, and its vector."""

    blob_sha: str
    chunk_no: int
    chunker_version: int
    model_key: str
    byte_start: int
    byte_end: int
    text: str
    embedding: list[float]


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create the extension, schema, and tables if they aren't there.

    This is how the local evaluation CLI and the tests get a database. A deployed index gets
    its schema from a migration instead — see this package's README on what deployment needs.
    """
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        await connection.run_sync(Base.metadata.create_all)


async def cached_blobs(
    session: AsyncSession, blob_shas: Sequence[str], *, chunker_version: int, model_key: str
) -> set[str]:
    """Which of `blob_shas` already have chunks under this (chunker, model) regime."""
    if not blob_shas:
        return set()
    result = await session.execute(
        select(Chunk.blob_sha)
        .where(Chunk.blob_sha.in_(blob_shas))
        .where(Chunk.chunker_version == chunker_version)
        .where(Chunk.model_key == model_key)
        .distinct()
    )
    return set(result.scalars())


async def insert_chunks(session: AsyncSession, rows: Sequence[ChunkRow], *, now: datetime.datetime) -> None:
    """Insert freshly embedded chunks, refreshing `last_seen_at` on any that already existed."""
    if not rows:
        return
    statement = pg_insert(Chunk).values([{**asdict(row), "last_seen_at": now} for row in rows])
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["blob_sha", "chunk_no", "chunker_version", "model_key"], set_={"last_seen_at": now}
        )
    )


async def touch_blobs(
    session: AsyncSession, blob_shas: Sequence[str], *, chunker_version: int, model_key: str, now: datetime.datetime
) -> None:
    """Mark cached chunks as still present at the tip, so eviction can use `last_seen_at`."""
    if not blob_shas:
        return
    await session.execute(
        update(Chunk)
        .where(Chunk.blob_sha.in_(blob_shas))
        .where(Chunk.chunker_version == chunker_version)
        .where(Chunk.model_key == model_key)
        .values(last_seen_at=now)
    )


async def replace_tip(
    session: AsyncSession,
    entries: Sequence[TipEntry],
    *,
    commit_sha: str,
    branch: str,
    chunker_version: int,
    model_key: str,
    now: datetime.datetime,
) -> None:
    """Swap the searchable set to `entries` — the caller's transaction makes it atomic.

    No rename, no DDL: the tip is data. A reader either sees the whole previous commit or the
    whole new one, and a sync that dies halfway leaves the previous one intact.
    """
    await session.execute(delete(TipRow))
    if entries:
        await session.execute(insert(TipRow), [{"path": entry.path, "blob_sha": entry.blob_sha} for entry in entries])
    state = {
        "id": 1,
        "commit_sha": commit_sha,
        "branch": branch,
        "chunker_version": chunker_version,
        "model_key": model_key,
        "synced_at": now,
    }
    await session.execute(pg_insert(SyncState).values(**state).on_conflict_do_update(index_elements=["id"], set_=state))


async def current_state(session: AsyncSession) -> SyncState | None:
    """What the searchable set currently holds, or None before the first sync."""
    return await session.get(SyncState, 1)


async def search(
    session: AsyncSession,
    embedding: Sequence[float],
    *,
    chunker_version: int,
    model_key: str,
    limit: int,
    path_prefix: str | None = None,
) -> list[SearchHit]:
    result = await session.execute(
        _SEARCH_SQL,
        {
            "chunker_version": chunker_version,
            "model_key": model_key,
            "path_prefix": path_prefix,
            "query": f"[{','.join(map(str, embedding))}]",
            "limit": limit,
        },
    )
    return [SearchHit(**row) for row in result.mappings()]


async def read_indexed_text(session: AsyncSession, path: str, *, chunker_version: int, model_key: str) -> str | None:
    """The indexed spans of `path` at the tip, concatenated, or None if it isn't indexed.

    Not byte-exact: whitespace-only spans are never chunked, so runs of blank lines between
    chunks are absent. It is the text search actually matched against, which is what a caller
    reading a hit wants; anyone needing the real file should read it from git.
    """
    result = await session.execute(
        select(Chunk.text)
        .join(TipRow, TipRow.blob_sha == Chunk.blob_sha)
        .where(TipRow.path == path)
        .where(Chunk.chunker_version == chunker_version)
        .where(Chunk.model_key == model_key)
        .order_by(Chunk.chunk_no)
    )
    chunks = list(result.scalars())
    return "".join(chunks) if chunks else None
