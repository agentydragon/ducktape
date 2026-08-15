"""Database operations over the haku index.

Chunk reads and writes are corpus-scoped everywhere: `corpus` is part of the cache key, part of
every search's filter, and part of every join. Content addresses from two corpora are never
comparable, so a helper that forgot it would silently look up the wrong namespace.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from haku.state_index.chat_corpus import MessageChunk
from haku.state_index.git_tree import TipEntry
from haku.state_index.schema import (
    SCHEMA,
    Base,
    ChatChunk,
    ChatChunkMessage,
    ChatSessionState,
    Chunk,
    Corpus,
    GitSyncState,
    GitTipEntry,
)

# The filters must be applied before the distance operator sees a row: `chunks` may hold
# vectors of several dimensions (one per model regime) and pgvector errors when comparing
# across dimensions. MATERIALIZED pins that evaluation order rather than trusting the planner.
_GIT_SEARCH_SQL = text("""
    WITH candidates AS MATERIALIZED (
        SELECT t.path, t.blob_sha, c.chunk_no, c.byte_start, c.byte_end, c.text, c.embedding
        FROM state_index.git_tip t
        JOIN state_index.chunks c ON c.corpus = :corpus AND c.content_sha = t.blob_sha
        WHERE c.chunker_key = :chunker_key
          AND c.model_key = :model_key
          AND (CAST(:path_prefix AS text) IS NULL OR starts_with(t.path, CAST(:path_prefix AS text)))
    )
    SELECT path, blob_sha, chunk_no, byte_start, byte_end, text,
           1 - (embedding <=> CAST(:query AS vector)) AS score
    FROM candidates
    ORDER BY embedding <=> CAST(:query AS vector)
    LIMIT :limit
""")

# The message list is gathered after the ranking, not joined into it: it is what the caller
# drills into, and aggregating it over every candidate window would be work thrown away.
_CHAT_SEARCH_SQL = text("""
    WITH candidates AS MATERIALIZED (
        SELECT w.session_id, w.chunk_no, w.first_message_at, w.last_message_at, c.text, c.embedding
        FROM state_index.chat_chunks w
        JOIN state_index.chunks c ON c.corpus = :corpus AND c.content_sha = w.content_sha
        WHERE c.chunker_key = :chunker_key
          AND c.model_key = :model_key
          AND (CAST(:session_id AS uuid) IS NULL OR w.session_id = CAST(:session_id AS uuid))
    ), ranked AS (
        SELECT session_id, chunk_no, first_message_at, last_message_at, text,
               1 - (embedding <=> CAST(:query AS vector)) AS score
        FROM candidates
        ORDER BY embedding <=> CAST(:query AS vector)
        LIMIT :limit
    )
    SELECT ranked.*,
           ARRAY(
               SELECT m.message_id FROM state_index.chat_chunk_messages m
               WHERE m.session_id = ranked.session_id AND m.chunk_no = ranked.chunk_no
               ORDER BY m.ordinal
           ) AS message_ids
    FROM ranked
    ORDER BY score DESC
""")


@dataclass(frozen=True, slots=True)
class GitSearchHit:
    path: str
    # The content itself, not just where it sat: a caller with a clone can read the exact bytes
    # back with `git cat-file`, and a path alone would have moved by the time they did.
    blob_sha: str
    chunk_no: int
    byte_start: int
    byte_end: int
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class ChatSearchHit:
    session_id: UUID
    chunk_no: int
    message_ids: list[UUID]
    first_message_at: datetime.datetime
    last_message_at: datetime.datetime
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class GitIndexSummary:
    files: int
    chunks: int


@dataclass(frozen=True, slots=True)
class ChunkCounts:
    """How much of a corpus a search can currently reach, and how much it cannot.

    `superseded` is chunks under a chunker or model that is no longer current: they are still
    cached against a rollback, but nothing under the live regime joins them, so they answer
    nothing until the sync re-embeds their content.
    """

    current: int
    superseded: int


@dataclass(frozen=True, slots=True)
class ChatIndexSummary:
    sessions: int
    chunks: int
    last_indexed_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class ChunkRow:
    """A chunk ready to be written: the cache key, the span, and its vector."""

    corpus: Corpus
    content_sha: str
    chunk_no: int
    chunker_key: str
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


async def cached_content(
    session: AsyncSession, corpus: Corpus, content_shas: Sequence[str], *, chunker_key: str, model_key: str
) -> set[str]:
    """Which of `content_shas` already have chunks under this (corpus, chunker, model) regime."""
    if not content_shas:
        return set()
    result = await session.execute(
        select(Chunk.content_sha)
        .where(Chunk.corpus == corpus)
        .where(Chunk.content_sha.in_(content_shas))
        .where(Chunk.chunker_key == chunker_key)
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
            index_elements=["corpus", "content_sha", "chunk_no", "chunker_key", "model_key"], set_={"last_seen_at": now}
        )
    )


async def touch_content(
    session: AsyncSession,
    corpus: Corpus,
    content_shas: Sequence[str],
    *,
    chunker_key: str,
    model_key: str,
    now: datetime.datetime,
) -> None:
    """Mark cached chunks as still reachable, so eviction can use `last_seen_at`."""
    if not content_shas:
        return
    await session.execute(
        update(Chunk)
        .where(Chunk.corpus == corpus)
        .where(Chunk.content_sha.in_(content_shas))
        .where(Chunk.chunker_key == chunker_key)
        .where(Chunk.model_key == model_key)
        .values(last_seen_at=now)
    )


async def replace_tip(
    session: AsyncSession,
    entries: Sequence[TipEntry],
    *,
    commit_sha: str,
    branch: str,
    chunker_key: str,
    model_key: str,
    now: datetime.datetime,
) -> None:
    """Swap the searchable set to `entries` — the caller's transaction makes it atomic.

    No rename, no DDL: the tip is data. A reader either sees the whole previous commit or the
    whole new one, and a sync that dies halfway leaves the previous one intact.
    """
    await session.execute(delete(GitTipEntry))
    if entries:
        await session.execute(
            insert(GitTipEntry), [{"path": entry.path, "blob_sha": entry.blob_sha} for entry in entries]
        )
    state = {
        "id": 1,
        "commit_sha": commit_sha,
        "branch": branch,
        "chunker_key": chunker_key,
        "model_key": model_key,
        "synced_at": now,
    }
    await session.execute(
        pg_insert(GitSyncState).values(**state).on_conflict_do_update(index_elements=["id"], set_=state)
    )


async def current_git_state(session: AsyncSession) -> GitSyncState | None:
    """What the searchable git set currently holds, or None before the first sync."""
    return await session.get(GitSyncState, 1)


async def search_git(
    session: AsyncSession,
    embedding: Sequence[float],
    *,
    chunker_key: str,
    model_key: str,
    limit: int,
    path_prefix: str | None = None,
) -> list[GitSearchHit]:
    result = await session.execute(
        _GIT_SEARCH_SQL,
        {
            "corpus": Corpus.GIT,
            "chunker_key": chunker_key,
            "model_key": model_key,
            "path_prefix": path_prefix,
            "query": f"[{','.join(map(str, embedding))}]",
            "limit": limit,
        },
    )
    return [GitSearchHit(**row) for row in result.mappings()]


async def read_indexed_text(session: AsyncSession, path: str, *, chunker_key: str, model_key: str) -> str | None:
    """The indexed spans of `path` at the tip, concatenated, or None if it isn't indexed.

    Not byte-exact: whitespace-only spans are never chunked, so runs of blank lines between
    chunks are absent. It is the text search actually matched against, which is what a caller
    reading a hit wants; anyone needing the real file should read it from git.
    """
    result = await session.execute(
        select(Chunk.text)
        .join(GitTipEntry, GitTipEntry.blob_sha == Chunk.content_sha)
        .where(Chunk.corpus == Corpus.GIT)
        .where(GitTipEntry.path == path)
        .where(Chunk.chunker_key == chunker_key)
        .where(Chunk.model_key == model_key)
        .order_by(Chunk.chunk_no)
    )
    chunks = list(result.scalars())
    return "".join(chunks) if chunks else None


async def chat_session_states(session: AsyncSession) -> dict[UUID, ChatSessionState]:
    """Every session's indexed shape, keyed by session, for the sync to compare against."""
    result = await session.execute(select(ChatSessionState))
    return {state.session_id: state for state in result.scalars()}


async def replace_chat_session(
    session: AsyncSession,
    session_id: UUID,
    chunks: Sequence[MessageChunk],
    *,
    message_count: int,
    last_message_at: datetime.datetime,
    chunker_key: str,
    model_key: str,
    now: datetime.datetime,
) -> None:
    """Swap one session's windows to `chunks`, atomic within the caller's transaction.

    Wholesale per session rather than incremental: a session's trailing window changes shape as
    it grows, so "append the new ones" would leave a stale partial window searchable next to the
    one that supersedes it. Re-windowing is cheap because the vectors are cached by content.
    """
    await session.execute(delete(ChatChunk).where(ChatChunk.session_id == session_id))
    if chunks:
        await session.execute(
            insert(ChatChunk),
            [
                {
                    "session_id": session_id,
                    "chunk_no": chunk.chunk_no,
                    "content_sha": chunk.content_sha,
                    "first_message_at": chunk.first_message_at,
                    "last_message_at": chunk.last_message_at,
                }
                for chunk in chunks
            ],
        )
        await session.execute(
            insert(ChatChunkMessage),
            [
                {"session_id": session_id, "chunk_no": chunk.chunk_no, "ordinal": ordinal, "message_id": message_id}
                for chunk in chunks
                for ordinal, message_id in enumerate(chunk.message_ids)
            ],
        )
    state = {
        "session_id": session_id,
        "message_count": message_count,
        "last_message_at": last_message_at,
        "chunker_key": chunker_key,
        "model_key": model_key,
        "indexed_at": now,
    }
    await session.execute(
        pg_insert(ChatSessionState).values(**state).on_conflict_do_update(index_elements=["session_id"], set_=state)
    )


async def forget_chat_sessions(session: AsyncSession, session_ids: Sequence[UUID]) -> None:
    """Drop sessions that are no longer in the source.

    The git corpus gets this for free — its search joins the tip, so anything not at the tip is
    already unreachable. Chat windows are reachable until deleted, so a session the console has
    dropped stays searchable unless the sync sweeps it.
    """
    if not session_ids:
        return
    await session.execute(delete(ChatChunk).where(ChatChunk.session_id.in_(session_ids)))
    await session.execute(delete(ChatSessionState).where(ChatSessionState.session_id.in_(session_ids)))


async def search_chat(
    session: AsyncSession,
    embedding: Sequence[float],
    *,
    chunker_key: str,
    model_key: str,
    limit: int,
    session_id: UUID | None = None,
) -> list[ChatSearchHit]:
    result = await session.execute(
        _CHAT_SEARCH_SQL,
        {
            "corpus": Corpus.CHAT,
            "chunker_key": chunker_key,
            "model_key": model_key,
            "session_id": session_id,
            "query": f"[{','.join(map(str, embedding))}]",
            "limit": limit,
        },
    )
    return [ChatSearchHit(**row) for row in result.mappings()]


async def git_index_summary(session: AsyncSession, *, chunker_key: str, model_key: str) -> GitIndexSummary:
    """What the searchable git set holds: the tree's size, and the chunks a search can reach.

    `chunks` joins the tip rather than counting the corpus, so it excludes both the cache's
    departed content and files that are legitimately never chunked (binaries, oversized blobs).
    """
    files = (await session.execute(select(func.count()).select_from(GitTipEntry))).scalar_one()
    chunks = (
        await session.execute(
            select(func.count())
            .select_from(GitTipEntry)
            .join(Chunk, Chunk.content_sha == GitTipEntry.blob_sha)
            .where(Chunk.corpus == Corpus.GIT)
            .where(Chunk.chunker_key == chunker_key)
            .where(Chunk.model_key == model_key)
        )
    ).scalar_one()
    return GitIndexSummary(files=files, chunks=chunks)


async def chunk_counts(session: AsyncSession, corpus: Corpus, *, chunker_key: str, model_key: str) -> ChunkCounts:
    """How many of a corpus's chunks are under the live regime, and how many are left behind."""
    current_regime = (Chunk.chunker_key == chunker_key) & (Chunk.model_key == model_key)
    current, superseded = (
        await session.execute(
            select(func.count().filter(current_regime), func.count().filter(~current_regime))
            .select_from(Chunk)
            .where(Chunk.corpus == corpus)
        )
    ).one()
    return ChunkCounts(current=current, superseded=superseded)


async def chat_index_summary(session: AsyncSession) -> ChatIndexSummary:
    """What the searchable chat set currently holds."""
    sessions, last_indexed_at = (
        await session.execute(select(func.count(), func.max(ChatSessionState.indexed_at)))
    ).one()
    chunks = (await session.execute(select(func.count()).select_from(ChatChunk))).scalar_one()
    return ChatIndexSummary(sessions=sessions, chunks=chunks, last_indexed_at=last_indexed_at)
