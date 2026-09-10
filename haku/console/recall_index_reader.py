"""Bind configured recall indexes to the console's database and embedder.

The deploy configuration is the source registry. This adapter translates configured index types
into source-specific storage operations without inventing special names for any index.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.tools.recall_index import GitIndexStatus, GitSource, IndexStatus, SearchHit, SearchResults
from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.config import ConfiguredRecallIndex, GitRecallIndexDefinition
from haku.recall_index.embedder import Embedder
from haku.recall_index.schema import IndexType
from haku.recall_index.store import chunk_counts, current_git_state, git_index_summary, search_git


class PostgresIndexSearcher:
    """Configured logical-index search over the console's Postgres database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        indexes: tuple[ConfiguredRecallIndex, ...],
        budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._indexes = {index.index_id: index for index in indexes}
        self._budget = budget

    def _selected(self, index_ids: tuple[str, ...]) -> tuple[ConfiguredRecallIndex, ...]:
        unknown = sorted(set(index_ids) - self._indexes.keys())
        if unknown:
            raise ValueError(f"unknown configured recall indexes: {', '.join(unknown)}")
        return tuple(self._indexes[index_id] for index_id in dict.fromkeys(index_ids))

    async def search(self, query: str, *, index_id: str, limit: int) -> SearchResults:
        selected = self._selected((index_id,))
        embedding = await self._embedder.embed_query(query)
        hits: list[SearchHit] = []
        async with self._sessions() as session:
            for index in selected:
                hits.extend(await self._search_git(session, index, embedding, limit=limit))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        status = await self.status(index_ids=(index_id,))
        selected_ids = {index.index_id for index in selected}
        return SearchResults(
            hits=hits[:limit],
            index=status
            if any(_is_behind(item) and item.index_id in selected_ids for item in status.indexes)
            else None,
        )

    async def _search_git(
        self, session: AsyncSession, index: GitRecallIndexDefinition, embedding: list[float], *, limit: int
    ) -> list[SearchHit]:
        state = await current_git_state(session, index.index_id)
        if state is None:
            return []
        found = await search_git(
            session,
            embedding,
            index_id=index.index_id,
            model_key=self._embedder.model_key,
            limit=limit,
            path_prefix=None,
            budget=self._budget,
        )
        return [
            SearchHit(
                score=hit.score,
                content=hit.text,
                source=GitSource(
                    index_id=index.index_id,
                    path=hit.path,
                    commit_sha=state.commit_sha,
                    blob_sha=hit.blob_sha,
                    byte_start=hit.byte_start,
                    byte_end=hit.byte_end,
                ),
            )
            for hit in found
            if state.commit_sha is not None
        ]

    async def status(self, *, index_ids: tuple[str, ...]) -> IndexStatus:
        model_key = self._embedder.model_key
        statuses: list[GitIndexStatus] = []
        async with self._sessions() as session:
            for index in self._selected(index_ids):
                state = await current_git_state(session, index.index_id)
                summary = await git_index_summary(session, index_id=index.index_id, budget=self._budget)
                counts = await chunk_counts(
                    session, IndexType.GIT, index_id=index.index_id, model_key=model_key, budget=self._budget
                )
                statuses.append(
                    GitIndexStatus(
                        index_id=index.index_id,
                        indexed_commit=None if state is None else state.commit_sha,
                        remote_commit=None if state is None else state.remote_commit,
                        remote_seen_at=None if state is None else state.remote_seen_at,
                        branch=None if state is None else state.branch,
                        indexed_at=None if state is None else state.synced_at,
                        files=summary.files,
                        chunks=summary.chunks,
                        embedded_chunks=counts.current,
                        pending_chunks=counts.pending,
                        superseded_chunks=counts.superseded,
                    )
                )
        return IndexStatus(indexes=statuses)


def _is_behind(status: GitIndexStatus) -> bool:
    return status.indexed_commit != status.remote_commit or status.pending_chunks > 0
