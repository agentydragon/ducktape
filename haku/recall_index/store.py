"""Database operations over Haku's content-addressed semantic index.

Source-specific rows describe where content occurred.  ``contents`` holds the exact normalized
text globally, and ``content_embeddings`` holds that text's vector for one embedding model.  This
keeps provenance local while making semantic materialization reusable across every index.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import batched

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget, git_chunker_key
from haku.recall_index.git_tree import TipEntry
from haku.recall_index.schema import (
    SCHEMA,
    Base,
    Content,
    ContentEmbedding,
    GitChunk,
    GitSyncState,
    GitTipEntry,
    IndexType,
    RecallIndex,
)

# Materializing candidate rows before applying the distance operator is load-bearing: embeddings
# for different model keys may have different dimensions, and pgvector refuses to compare them.
_MAX_IN_VALUES = 10_000

_GIT_SEARCH_SQL = text(f"""
    WITH candidates AS MATERIALIZED (
        SELECT t.path, t.blob_sha, g.byte_start, g.byte_end, c.content AS text, e.embedding
        FROM {SCHEMA}.git_tip t
        JOIN {SCHEMA}.git_chunks g ON g.index_id = t.index_id AND g.blob_sha = t.blob_sha
        JOIN {SCHEMA}.contents c ON c.content_sha = g.content_sha
        JOIN {SCHEMA}.content_embeddings e ON e.content_sha = c.content_sha
        WHERE t.index_id = :index_id
          AND g.chunker_key = :chunker_key
          AND e.model_key = :model_key
          AND (CAST(:path_prefix AS text) IS NULL OR starts_with(t.path, CAST(:path_prefix AS text)))
    )
    SELECT path, blob_sha, byte_start, byte_end, text,
           1 - (embedding <=> CAST(:query AS halfvec)) AS score
    FROM candidates
    ORDER BY embedding <=> CAST(:query AS halfvec)
    LIMIT :limit
""")


@dataclass(frozen=True, slots=True)
class GitSearchHit:
    path: str
    blob_sha: str
    byte_start: int
    byte_end: int
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class GitIndexSummary:
    files: int
    chunks: int


@dataclass(frozen=True, slots=True)
class ChunkCounts:
    current: int
    pending: int
    superseded: int


@dataclass(frozen=True, slots=True)
class ContentEmbeddingRow:
    """One exact content value and the vector a model produced for it."""

    content_sha: str
    content: str
    model_key: str
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class ContentRow:
    """One globally-addressed string awaiting zero or more model embeddings."""

    content_sha: str
    content: str


@dataclass(frozen=True, slots=True)
class GitChunkRow:
    """One source occurrence of globally-addressed content in a Git blob."""

    index_id: str
    blob_sha: str
    chunker_key: str
    byte_start: int
    byte_end: int
    content_sha: str


def chunker_key_for(index_type: IndexType, budget: ChunkBudget = DEFAULT_CHUNK_BUDGET) -> str:
    """Which chunker regime is current for one index type."""
    match index_type:
        case IndexType.GIT:
            return git_chunker_key(budget)


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create the extension, schema, and tables for local evaluation and tests."""
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        await connection.run_sync(Base.metadata.create_all)


async def register_index(session: AsyncSession, index_id: str, *, index_type: IndexType) -> None:
    """Register one explicitly configured index before reading or writing its occurrences."""
    existing_index = await session.get(RecallIndex, index_id)
    if existing_index is not None and existing_index.index_type != index_type.value:
        raise ValueError(
            f"configured index {index_id!r} is {index_type.value!r}, but storage records {existing_index.index_type!r}"
        )
    await session.execute(
        pg_insert(RecallIndex).values(index_id=index_id, index_type=index_type.value).on_conflict_do_nothing()
    )


async def pending_content(session: AsyncSession, *, model_key: str, limit: int) -> list[ContentRow]:
    """Claim one batch of globally queued content with no vector for ``model_key`` yet.

    Sources populate ``contents`` independently of an embedding endpoint. This is therefore the
    shared work queue across Git and chat sources, and across every configured logical index.
    ``FOR UPDATE OF contents SKIP LOCKED`` makes the batch a claim for the lifetime of the
    caller's transaction: concurrent claimers take disjoint batches instead of embedding the
    same rows, and a crashed claimer's rows return to the queue with its rollback. Conflict-safe
    vector insertion stays the backstop for a claimless reader — a replica of a release that
    still selected without locking can at worst repeat an embedding request, never publish a
    conflicting result.
    """
    result = await session.execute(
        select(Content.content_sha, Content.content)
        .outerjoin(
            ContentEmbedding,
            (ContentEmbedding.content_sha == Content.content_sha) & (ContentEmbedding.model_key == model_key),
        )
        .where(ContentEmbedding.content_sha.is_(None))
        .order_by(Content.created_at, Content.content_sha)
        .limit(limit)
        # `of=Content`: only the queue side may be locked — `content_embeddings` is the nullable
        # side of the anti-join, which Postgres refuses to lock.
        .with_for_update(of=Content, skip_locked=True)
    )
    return [ContentRow(content_sha=content_sha, content=content) for content_sha, content in result]


async def git_chunked_blobs(
    session: AsyncSession, index_id: str, blob_shas: Iterable[str], *, chunker_key: str
) -> set[str]:
    """Which Git blobs already have their source occurrences under this chunker regime."""
    addresses = sorted(set(blob_shas))
    if not addresses:
        return set()
    chunked: set[str] = set()
    for addresses_batch in batched(addresses, _MAX_IN_VALUES, strict=False):
        result = await session.execute(
            select(GitChunk.blob_sha)
            .where(GitChunk.index_id == index_id)
            .where(GitChunk.blob_sha.in_(addresses_batch))
            .where(GitChunk.chunker_key == chunker_key)
            .distinct()
        )
        chunked.update(result.scalars())
    return chunked


def _content_map(rows: Iterable[tuple[str, str]]) -> dict[str, str]:
    content_by_sha: dict[str, str] = {}
    for address, content in rows:
        previous = content_by_sha.setdefault(address, content)
        if previous != content:
            raise AssertionError(f"content address collision: {address}")
    return content_by_sha


async def insert_contents(session: AsyncSession, rows: Iterable[ContentRow]) -> None:
    """Persist source material without requiring an embedding provider.

    The content table is both the source-independent deduplication layer and the embedding
    worker's durable queue. Existing bytes under an address are verified rather than silently
    accepted: a hash collision must fail before it can cross source boundaries.
    """
    content_by_sha = _content_map((row.content_sha, row.content) for row in rows)
    if not content_by_sha:
        return
    for addresses_batch in batched(content_by_sha, _MAX_IN_VALUES, strict=False):
        existing = await session.execute(
            select(Content.content_sha, Content.content).where(Content.content_sha.in_(addresses_batch))
        )
        for address, content in existing:
            if content_by_sha[address] != content:
                raise AssertionError(f"content address collision: {address}")
        await session.execute(
            pg_insert(Content).on_conflict_do_nothing(index_elements=["content_sha"]),
            [{"content_sha": address, "content": content_by_sha[address]} for address in addresses_batch],
        )


async def insert_content_embeddings(session: AsyncSession, rows: Sequence[ContentEmbeddingRow]) -> None:
    """Persist vectors, retaining an existing vector as the authoritative result.

    The two inserts are conflict-safe, so independently-running sync workers cannot duplicate
    durable rows.  Callers de-duplicate misses before asking an embedding provider; a rare race
    may still compute the same vector twice, but cannot publish two conflicting representations.
    """
    if not rows:
        return
    await insert_contents(session, (ContentRow(content_sha=row.content_sha, content=row.content) for row in rows))
    embedding_statement = pg_insert(ContentEmbedding).on_conflict_do_nothing(
        index_elements=["content_sha", "model_key"]
    )
    await session.execute(
        embedding_statement,
        [{"content_sha": row.content_sha, "model_key": row.model_key, "embedding": row.embedding} for row in rows],
    )


async def insert_git_chunks(session: AsyncSession, rows: Sequence[GitChunkRow]) -> None:
    """Persist Git source occurrences after their content rows exist."""
    if not rows:
        return
    statement = pg_insert(GitChunk).on_conflict_do_nothing(
        index_elements=["index_id", "blob_sha", "chunker_key", "byte_start"]
    )
    await session.execute(
        statement,
        [
            {
                "index_id": row.index_id,
                "blob_sha": row.blob_sha,
                "chunker_key": row.chunker_key,
                "byte_start": row.byte_start,
                "byte_end": row.byte_end,
                "content_sha": row.content_sha,
            }
            for row in rows
        ],
    )


async def replace_tip(
    session: AsyncSession,
    entries: Sequence[TipEntry],
    *,
    index_id: str,
    commit_sha: str,
    branch: str,
    chunker_key: str,
    now: datetime.datetime,
) -> None:
    """Atomically make one Git tree the searchable tip after its content is materialized."""
    await session.execute(delete(GitTipEntry).where(GitTipEntry.index_id == index_id))
    if entries:
        await session.execute(
            insert(GitTipEntry),
            [{"index_id": index_id, "path": entry.path, "blob_sha": entry.blob_sha} for entry in entries],
        )
    indexed = {"commit_sha": commit_sha, "branch": branch, "chunker_key": chunker_key, "synced_at": now}
    await session.execute(
        pg_insert(GitSyncState)
        .values(index_id=index_id, **indexed)
        .on_conflict_do_update(index_elements=["index_id"], set_=indexed)
    )


async def current_git_state(session: AsyncSession, index_id: str) -> GitSyncState | None:
    return await session.get(GitSyncState, index_id)


async def record_remote_tip(
    session: AsyncSession, commit_sha: str, *, index_id: str, branch: str, now: datetime.datetime
) -> None:
    values = {"index_id": index_id, "branch": branch, "remote_commit": commit_sha, "remote_seen_at": now}
    await session.execute(
        pg_insert(GitSyncState)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["index_id"], set_={"branch": branch, "remote_commit": commit_sha, "remote_seen_at": now}
        )
    )


async def search_git(
    session: AsyncSession,
    embedding: Sequence[float],
    *,
    index_id: str,
    model_key: str,
    limit: int,
    path_prefix: str | None = None,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> list[GitSearchHit]:
    result = await session.execute(
        _GIT_SEARCH_SQL,
        {
            "index_id": index_id,
            "chunker_key": chunker_key_for(IndexType.GIT, budget),
            "model_key": model_key,
            "path_prefix": path_prefix,
            "query": f"[{','.join(map(str, embedding))}]",
            "limit": limit,
        },
    )
    return [GitSearchHit(**row) for row in result.mappings()]


async def read_indexed_text(
    session: AsyncSession, path: str, *, index_id: str, model_key: str, budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
) -> str | None:
    """The current Git chunk contents at ``path``, concatenated in source-byte order."""
    result = await session.execute(
        select(Content.content)
        .join(GitChunk, GitChunk.content_sha == Content.content_sha)
        .join(GitTipEntry, (GitTipEntry.index_id == GitChunk.index_id) & (GitTipEntry.blob_sha == GitChunk.blob_sha))
        .join(
            ContentEmbedding,
            (ContentEmbedding.content_sha == Content.content_sha) & (ContentEmbedding.model_key == model_key),
        )
        .where(GitTipEntry.index_id == index_id, GitTipEntry.path == path)
        .where(GitChunk.chunker_key == chunker_key_for(IndexType.GIT, budget))
        .order_by(GitChunk.byte_start)
    )
    chunks = list(result.scalars())
    return "".join(chunks) if chunks else None


async def git_index_summary(
    session: AsyncSession, *, index_id: str, budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
) -> GitIndexSummary:
    files = (
        await session.execute(select(func.count()).select_from(GitTipEntry).where(GitTipEntry.index_id == index_id))
    ).scalar_one()
    chunks = (
        await session.execute(
            select(func.count())
            .select_from(GitTipEntry)
            .join(GitChunk, (GitChunk.index_id == GitTipEntry.index_id) & (GitChunk.blob_sha == GitTipEntry.blob_sha))
            .where(GitTipEntry.index_id == index_id, GitChunk.chunker_key == chunker_key_for(IndexType.GIT, budget))
        )
    ).scalar_one()
    return GitIndexSummary(files=files, chunks=chunks)


async def chunk_counts(
    session: AsyncSession,
    index_type: IndexType,
    *,
    index_id: str,
    model_key: str,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> ChunkCounts:
    """Count current source occurrences, queued ones, and obsolete chunker regimes."""
    match index_type:
        case IndexType.GIT:
            total = (
                await session.execute(select(func.count()).select_from(GitChunk).where(GitChunk.index_id == index_id))
            ).scalar_one()
            source_current = (
                await session.execute(
                    select(func.count())
                    .select_from(GitTipEntry)
                    .join(
                        GitChunk,
                        (GitChunk.index_id == GitTipEntry.index_id) & (GitChunk.blob_sha == GitTipEntry.blob_sha),
                    )
                    .where(
                        GitTipEntry.index_id == index_id, GitChunk.chunker_key == chunker_key_for(IndexType.GIT, budget)
                    )
                )
            ).scalar_one()
            current = (
                await session.execute(
                    select(func.count())
                    .select_from(GitTipEntry)
                    .join(
                        GitChunk,
                        (GitChunk.index_id == GitTipEntry.index_id) & (GitChunk.blob_sha == GitTipEntry.blob_sha),
                    )
                    .join(
                        ContentEmbedding,
                        (ContentEmbedding.content_sha == GitChunk.content_sha)
                        & (ContentEmbedding.model_key == model_key),
                    )
                    .where(
                        GitTipEntry.index_id == index_id, GitChunk.chunker_key == chunker_key_for(IndexType.GIT, budget)
                    )
                )
            ).scalar_one()
    return ChunkCounts(current=current, pending=source_current - current, superseded=total - source_current)
