"""Asking one named index a question: embed the text, then rank its chunks against it.

The pair with `store.search_git`/`search_chat`, which take a vector: a caller searching multiple indexes embeds once and calls those, and everyone else asks in words and calls these. Either
way the query's regime is derived, never passed — a query embedded by one model or chunked to
one budget can only be compared against chunks written under the same, and a mismatch returns
an empty result rather than an error, which reads as "never discussed".
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.embedder import Embedder
from haku.recall_index.store import ChatSearchHit, GitSearchHit, search_chat, search_git


async def query_git(
    session: AsyncSession,
    embedder: Embedder,
    text: str,
    *,
    index_id: str,
    limit: int,
    path_prefix: str | None = None,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> list[GitSearchHit]:
    return await search_git(
        session,
        await embedder.embed_query(text),
        index_id=index_id,
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
    index_id: str,
    limit: int,
    readable_profiles: Sequence[str] | None,
    session_id: UUID | None = None,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> list[ChatSearchHit]:
    return await search_chat(
        session,
        await embedder.embed_query(text),
        index_id=index_id,
        model_key=embedder.model_key,
        limit=limit,
        readable_profiles=readable_profiles,
        session_id=session_id,
        budget=budget,
    )
