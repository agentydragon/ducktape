"""Binds the haku index to the console's database and embedder for the `haku_index` tools.

The index lives in the console's own Postgres — the conversations corpus is built from
`claude_chat_messages`, so it could not live anywhere else — and this is where that plumbing sits
rather than in `haku/state_index`, which stays a library with no opinion about who runs it.

This is also where the tool surface's vocabulary meets the storage's: `haku_state` and
`conversations` are what a caller asks for, `git` and `chat` are how they are stored, and the
mapping lives here and nowhere else.

**Query embedding runs off the event loop.** It is CPU work in-process (onnxruntime, no network,
because an embedder that is sometimes unreachable would take search down and not just indexing),
and a few tens of milliseconds on the shared loop is the console's whole latency budget for an
operator API call.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import ClaudeChatSession
from haku.console.tools.state_index import (
    ConversationSource,
    ConversationsStatus,
    HakuStateSource,
    HakuStateStatus,
    IndexStatus,
    SearchCorpus,
    SearchHit,
)
from haku.state_index.chat_corpus import CHAT_CHUNKER_VERSION
from haku.state_index.chat_source import session_shapes
from haku.state_index.chunking import CHUNKER_VERSION
from haku.state_index.embedder import Embedder
from haku.state_index.schema import Corpus
from haku.state_index.store import (
    chat_index_summary,
    chat_session_states,
    chunk_counts,
    current_git_state,
    git_index_summary,
    search_chat,
    search_git,
)


class PostgresIndexSearcher:
    """`tools.state_index.IndexSearcher` over the console's database."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._sessions = sessions
        self._embedder = embedder

    async def search(
        self, query: str, *, corpus: SearchCorpus, limit: int, path_prefix: str | None, session_id: UUID | None
    ) -> list[SearchHit]:
        embedding = await asyncio.to_thread(self._embedder.embed_query, query)
        hits: list[SearchHit] = []
        async with self._sessions() as session:
            if corpus in (SearchCorpus.HAKU_STATE, SearchCorpus.ALL):
                hits.extend(await self._haku_state(session, embedding, limit=limit, path_prefix=path_prefix))
            if corpus in (SearchCorpus.CONVERSATIONS, SearchCorpus.ALL):
                hits.extend(await self._conversations(session, embedding, limit=limit, session_id=session_id))
        # Both corpora are embedded by the same model, so their cosine scores are comparable and a
        # single ranking is meaningful. Each corpus was asked for `limit`, so `all` re-cuts here
        # rather than returning twice as much as the caller asked for.
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    async def _haku_state(
        self, session: AsyncSession, embedding: list[float], *, limit: int, path_prefix: str | None
    ) -> list[SearchHit]:
        state = await current_git_state(session)
        if state is None:
            return []
        found = await search_git(
            session,
            embedding,
            chunker_version=CHUNKER_VERSION,
            model_key=self._embedder.model_key,
            limit=limit,
            path_prefix=path_prefix,
        )
        # `commit_sha` on every hit rather than once alongside them: a hit is a pointer, and a
        # pointer that needs a second field from its envelope to be resolvable is half a pointer.
        return [
            SearchHit(
                score=hit.score,
                snippet=hit.text,
                source=HakuStateSource(
                    path=hit.path,
                    commit_sha=state.commit_sha,
                    blob_sha=hit.blob_sha,
                    byte_start=hit.byte_start,
                    byte_end=hit.byte_end,
                ),
            )
            for hit in found
        ]

    async def _conversations(
        self, session: AsyncSession, embedding: list[float], *, limit: int, session_id: UUID | None
    ) -> list[SearchHit]:
        found = await search_chat(
            session,
            embedding,
            chunker_version=CHAT_CHUNKER_VERSION,
            model_key=self._embedder.model_key,
            limit=limit,
            session_id=session_id,
        )
        # The room a hit came from is the console's own binding, not the index's: the index knows
        # sessions, and which room a session served is `claude_chat_sessions.room_id`.
        rooms: dict[UUID, str | None] = {
            row.session_id: row.room_id
            for row in (
                await session.execute(
                    select(ClaudeChatSession.session_id, ClaudeChatSession.room_id).where(
                        ClaudeChatSession.session_id.in_({hit.session_id for hit in found})
                    )
                )
            ).all()
        }
        return [
            SearchHit(
                score=hit.score,
                snippet=hit.text,
                source=ConversationSource(
                    session_id=hit.session_id,
                    room_id=rooms.get(hit.session_id),
                    message_ids=hit.message_ids,
                    first_message_at=hit.first_message_at,
                    last_message_at=hit.last_message_at,
                ),
            )
            for hit in found
        ]

    async def status(self) -> IndexStatus:
        model_key = self._embedder.model_key
        async with self._sessions() as session:
            git_state = await current_git_state(session)
            haku_state: HakuStateStatus | None = None
            if git_state is not None:
                summary = await git_index_summary(session, chunker_version=CHUNKER_VERSION, model_key=model_key)
                counts = await chunk_counts(session, Corpus.GIT, chunker_version=CHUNKER_VERSION, model_key=model_key)
                haku_state = HakuStateStatus(
                    commit_sha=git_state.commit_sha,
                    branch=git_state.branch,
                    indexed_at=git_state.synced_at,
                    files=summary.files,
                    chunks=summary.chunks,
                    superseded_chunks=counts.superseded,
                )

            chat_summary = await chat_index_summary(session)
            chat_counts = await chunk_counts(
                session, Corpus.CHAT, chunker_version=CHAT_CHUNKER_VERSION, model_key=model_key
            )
            # The same two reads `sync_chat` opens with, diffed the same way — so what this
            # reports as waiting is exactly what a sync run would pick up.
            shapes = await session_shapes(session)
            states = await chat_session_states(session)
        stale = [
            shape
            for shape in shapes
            if (state := states.get(shape.session_id)) is None
            or (state.message_count, state.last_message_at, state.chunker_version, state.model_key)
            != (shape.message_count, shape.last_message_at, CHAT_CHUNKER_VERSION, model_key)
        ]
        unindexed = sum(
            shape.message_count - state.message_count
            # A regime change strands every message in the session, not just the new ones.
            if (state := states.get(shape.session_id)) is not None
            and (state.chunker_version, state.model_key) == (CHAT_CHUNKER_VERSION, model_key)
            else shape.message_count
            for shape in stale
        )
        newest_waiting = max((shape.last_message_at for shape in stale), default=None)
        return IndexStatus(
            haku_state=haku_state,
            conversations=ConversationsStatus(
                sessions=chat_summary.sessions,
                chunks=chat_summary.chunks,
                stale_sessions=len(stale),
                unindexed_messages=unindexed,
                lag_seconds=(
                    None
                    if newest_waiting is None
                    else (datetime.datetime.now(datetime.UTC) - newest_waiting).total_seconds()
                ),
                last_indexed_at=chat_summary.last_indexed_at,
                superseded_chunks=chat_counts.superseded,
            ),
        )
