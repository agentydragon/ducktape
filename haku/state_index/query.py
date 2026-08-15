"""Asking a corpus a question: embed the text, then rank that corpus's chunks against it.

The pair with `store.search_git`/`search_chat`, which take a vector: a caller searching both
corpora embeds once and calls those, and everyone else asks in words and calls these. Either
way the query's regime is derived, never passed — a query embedded by one model or chunked to
one budget can only be compared against chunks written under the same, and a mismatch returns
an empty result rather than an error, which reads as "never discussed".
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from haku.state_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.state_index.embedder import Embedder
from haku.state_index.store import ChatSearchHit, GitSearchHit, search_chat, search_git


async def query_git(
    session: AsyncSession,
    embedder: Embedder,
    text: str,
    *,
    limit: int,
    path_prefix: str | None = None,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> list[GitSearchHit]:
    return await search_git(
        session,
        await embedder.embed_query(text),
        model_key=embedder.model_key,
        limit=limit,
        path_prefix=path_prefix,
        budget=budget,
    )


async def query_chat(
    session: AsyncSession,
    embedder: Embedder,
    text: str,
    *,
    limit: int,
    session_id: UUID | None = None,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> list[ChatSearchHit]:
    return await search_chat(
        session,
        await embedder.embed_query(text),
        model_key=embedder.model_key,
        limit=limit,
        session_id=session_id,
        budget=budget,
    )
