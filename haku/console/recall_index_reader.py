"""Binds the haku index to the console's database and embedder for the `haku_index` tools.

The index lives in the console's own Postgres — the conversations corpus is built from
`session_messages`, so it could not live anywhere else — and this is where that plumbing sits
rather than in `haku/recall_index`, which stays a library with no opinion about who runs it.

This is also where the tool surface's vocabulary meets the storage's: `haku_state` and
`conversations` are what a caller asks for, `git` and `chat` are how they are stored, and the
mapping lives here and nowhere else.

**Embedding is a call to the embedding service**, not work in this process — which is also why a
search fails loudly when that service is down rather than returning nothing: an empty result reads
as "never discussed", and that is a different claim from "could not look".
"""

from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import Session
from haku.console.tools.recall_index import (
    ConversationSource,
    ConversationsStatus,
    HakuStateSource,
    HakuStateStatus,
    IndexStatus,
    SearchCorpus,
    SearchHit,
    SearchResults,
)
from haku.recall_index.chat_corpus import chat_chunker_key
from haku.recall_index.chat_source import session_shapes
from haku.recall_index.chat_sync import is_indexed
from haku.recall_index.embedder import Embedder
from haku.recall_index.schema import Corpus
from haku.recall_index.store import (
    chat_index_summary,
    chat_session_states,
    chunk_counts,
    current_git_state,
    git_index_summary,
    search_chat,
    search_git,
)

# Under this, a corpus is not behind — it is mid-pipeline. The chat sweep runs every minute and
# holds a session for thirty seconds after its last message, so a lag inside that window is the
# thing working, and reporting it on every search would train a reader to ignore the field.
_SETTLED_WITHIN = datetime.timedelta(minutes=2)


def _behind(status: IndexStatus, corpus: SearchCorpus) -> bool:
    """Whether a searched corpus holds less than its source does, by enough to explain a miss."""
    if corpus in (SearchCorpus.HAKU_STATE, SearchCorpus.ALL) and (
        status.haku_state.indexed_commit != status.haku_state.remote_commit
    ):
        return True
    lag = status.conversations.lag_seconds
    return corpus in (SearchCorpus.CONVERSATIONS, SearchCorpus.ALL) and (
        lag is not None and lag > _SETTLED_WITHIN.total_seconds()
    )


class PostgresIndexSearcher:
    """`tools.recall_index.IndexSearcher` over the console's database."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._sessions = sessions
        self._embedder = embedder

    async def search(
        self, query: str, *, corpus: SearchCorpus, limit: int, path_prefix: str | None, session_id: UUID | None
    ) -> SearchResults:
        embedding = await self._embedder.embed_query(query)
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
        # Status costs a handful of counts against a search that already paid for an embedding
        # round trip, and it is only attached when it changes what the result means: a thin answer
        # from a corpus that is behind is not evidence of absence, and the caller cannot tell the
        # two apart without the numbers.
        status = await self.status()
        return SearchResults(hits=hits[:limit], index=status if _behind(status, corpus) else None)

    async def _haku_state(
        self, session: AsyncSession, embedding: list[float], *, limit: int, path_prefix: str | None
    ) -> list[SearchHit]:
        state = await current_git_state(session)
        if state is None:
            return []
        found = await search_git(
            session, embedding, model_key=self._embedder.model_key, limit=limit, path_prefix=path_prefix
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
            session, embedding, model_key=self._embedder.model_key, limit=limit, session_id=session_id
        )
        # The room a hit came from is the console's own binding, not the index's: the index knows
        # sessions, and which room a session served is `sessions.room_id`.
        rooms: dict[UUID, str | None] = {
            row.session_id: row.room_id
            for row in (
                await session.execute(
                    select(Session.session_id, Session.room_id).where(
                        Session.session_id.in_({hit.session_id for hit in found})
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
            summary = await git_index_summary(session, model_key=model_key)
            counts = await chunk_counts(session, Corpus.GIT, model_key=model_key)
            haku_state = HakuStateStatus(
                indexed_commit=None if git_state is None else git_state.commit_sha,
                remote_commit=None if git_state is None else git_state.remote_commit,
                remote_seen_at=None if git_state is None else git_state.remote_seen_at,
                branch=None if git_state is None else git_state.branch,
                indexed_at=None if git_state is None else git_state.synced_at,
                files=summary.files,
                chunks=summary.chunks,
                embedded_chunks=counts.current,
                superseded_chunks=counts.superseded,
            )

            chat_summary = await chat_index_summary(session)
            chat_counts = await chunk_counts(session, Corpus.CHAT, model_key=model_key)
            # The same two reads `sync_chat` opens with, diffed by its own predicate — so what
            # this reports as waiting is what a sync run would pick up, by construction rather
            # than by two spellings agreeing.
            shapes = await session_shapes(session)
            states = await chat_session_states(session)
        stale = [shape for shape in shapes if not is_indexed(states.get(shape.session_id), shape, model_key=model_key)]
        unindexed = sum(
            shape.message_count - state.message_count
            # A regime change strands every message in the session, not just the new ones.
            if (state := states.get(shape.session_id)) is not None
            and (state.chunker_key, state.model_key) == (chat_chunker_key(), model_key)
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
