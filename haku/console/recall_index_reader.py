"""Bind configured recall indexes to the console's database and embedder.

The deploy configuration is the source registry. This adapter translates configured index types
into source-specific storage operations without inventing special names for any index.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.channels.surface import ChannelSurface
from haku.console.conversation_read_access import ConversationReadScope
from haku.console.database_schema import ChannelAttachmentRow, Session
from haku.console.tools.recall_index import (
    ChatIndexStatus,
    ChatSource,
    GitIndexStatus,
    GitSource,
    IndexStatus,
    SearchHit,
    SearchResults,
)
from haku.recall_index.chat_corpus import chat_chunker_key
from haku.recall_index.chat_source import session_shapes
from haku.recall_index.chat_sync import is_indexed
from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.config import ChatRecallIndexDefinition, ConfiguredRecallIndex, GitRecallIndexDefinition
from haku.recall_index.embedder import Embedder
from haku.recall_index.schema import IndexType
from haku.recall_index.store import (
    chat_index_summary,
    chat_session_states,
    chunk_counts,
    current_git_state,
    git_index_summary,
    search_chat,
    search_git,
)

_SETTLED_WITHIN = datetime.timedelta(minutes=2)


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

    async def search(
        self, query: str, *, index_id: str, limit: int, session_id: UUID | None, scope: ConversationReadScope
    ) -> SearchResults:
        selected = self._selected((index_id,))
        embedding = await self._embedder.embed_query(query)
        hits: list[SearchHit] = []
        async with self._sessions() as session:
            for index in selected:
                if isinstance(index, GitRecallIndexDefinition):
                    hits.extend(await self._search_git(session, index, embedding, limit=limit))
                else:
                    hits.extend(
                        await self._search_chat(
                            session, index, embedding, limit=limit, session_id=session_id, scope=scope
                        )
                    )
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

    async def _search_chat(
        self,
        session: AsyncSession,
        index: ChatRecallIndexDefinition,
        embedding: list[float],
        *,
        limit: int,
        session_id: UUID | None,
        scope: ConversationReadScope,
    ) -> list[SearchHit]:
        found = await search_chat(
            session,
            embedding,
            index_id=index.index_id,
            model_key=self._embedder.model_key,
            limit=limit,
            readable_profiles=scope.profile_filter,
            session_id=session_id,
            budget=self._budget,
        )
        rooms: dict[UUID, str] = {
            row.session_id: row.address
            for row in (
                await session.execute(
                    select(Session.session_id, ChannelAttachmentRow.address)
                    .join(ChannelAttachmentRow, ChannelAttachmentRow.conversation_id == Session.conversation_id)
                    .where(
                        Session.session_id.in_({hit.session_id for hit in found}),
                        ChannelAttachmentRow.surface == ChannelSurface.MATRIX,
                        ChannelAttachmentRow.detached_at.is_(None),
                    )
                )
            ).all()
        }
        return [
            SearchHit(
                score=hit.score,
                content=hit.text,
                source=ChatSource(
                    index_id=index.index_id,
                    session_id=hit.session_id,
                    conversation_id=hit.conversation_id,
                    room_id=rooms.get(hit.session_id),
                    message_ids=hit.message_ids,
                    first_message_at=hit.first_message_at,
                    last_message_at=hit.last_message_at,
                ),
            )
            for hit in found
        ]

    async def status(self, *, index_ids: tuple[str, ...]) -> IndexStatus:
        model_key = self._embedder.model_key
        statuses: list[GitIndexStatus | ChatIndexStatus] = []
        async with self._sessions() as session:
            for index in self._selected(index_ids):
                if isinstance(index, GitRecallIndexDefinition):
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
                else:
                    statuses.append(await self._chat_status(session, index, model_key))
        return IndexStatus(indexes=statuses)

    async def _chat_status(
        self, session: AsyncSession, index: ChatRecallIndexDefinition, model_key: str
    ) -> ChatIndexStatus:
        summary = await chat_index_summary(session, index.index_id)
        counts = await chunk_counts(
            session, IndexType.CHAT, index_id=index.index_id, model_key=model_key, budget=self._budget
        )
        shapes = await session_shapes(session)
        states = await chat_session_states(session, index.index_id)
        stale = [shape for shape in shapes if not is_indexed(states.get(shape.session_id), shape, budget=self._budget)]
        unindexed = sum(
            shape.message_count - state.message_count
            if (state := states.get(shape.session_id)) is not None
            and state.chunker_key == chat_chunker_key(self._budget)
            else shape.message_count
            for shape in stale
        )
        newest_waiting = max((shape.last_message_at for shape in stale), default=None)
        return ChatIndexStatus(
            index_id=index.index_id,
            sessions=summary.sessions,
            chunks=summary.chunks,
            embedded_chunks=counts.current,
            pending_chunks=counts.pending,
            stale_sessions=len(stale),
            unindexed_messages=unindexed,
            lag_seconds=(
                None
                if newest_waiting is None
                else (datetime.datetime.now(datetime.UTC) - newest_waiting).total_seconds()
            ),
            last_indexed_at=summary.last_indexed_at,
            superseded_chunks=counts.superseded,
        )


def _is_behind(status: GitIndexStatus | ChatIndexStatus) -> bool:
    if isinstance(status, GitIndexStatus):
        return status.indexed_commit != status.remote_commit or status.pending_chunks > 0
    return status.pending_chunks > 0 or (
        status.lag_seconds is not None and status.lag_seconds > _SETTLED_WITHIN.total_seconds()
    )
