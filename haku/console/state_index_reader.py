"""Binds the haku index to the console's database and embedder for the `haku_index` tools.

The index lives in the console's own Postgres — the conversations corpus is built from
`claude_chat_messages`, so it could not live anywhere else — and this is where that plumbing sits
rather than in `haku/state_index`, which stays a library with no opinion about who runs it.

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

from haku.console.database_schema import ClaudeChatMessage, ClaudeChatSession
from haku.console.tools.state_index import (
    ChatMessage,
    ConversationHit,
    ConversationsStatus,
    IndexStatus,
    NoteHit,
    NoteSearchResult,
    NotesStatus,
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
    read_indexed_text,
    search_chat,
    search_git,
)


class PostgresIndexSearcher:
    """`tools.state_index.IndexSearcher` over the console's database."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._sessions = sessions
        self._embedder = embedder

    async def _embed_query(self, query: str) -> list[float]:
        return await asyncio.to_thread(self._embedder.embed_query, query)

    async def search_notes(self, query: str, *, limit: int, path_prefix: str | None) -> NoteSearchResult | None:
        embedding = await self._embed_query(query)
        async with self._sessions() as session:
            state = await current_git_state(session)
            if state is None:
                return None
            hits = await search_git(
                session,
                embedding,
                chunker_version=CHUNKER_VERSION,
                model_key=self._embedder.model_key,
                limit=limit,
                path_prefix=path_prefix,
            )
        return NoteSearchResult(
            commit_sha=state.commit_sha,
            branch=state.branch,
            indexed_at=state.synced_at,
            hits=[
                NoteHit(
                    path=hit.path,
                    blob_sha=hit.blob_sha,
                    byte_start=hit.byte_start,
                    byte_end=hit.byte_end,
                    snippet=hit.text,
                    score=hit.score,
                )
                for hit in hits
            ],
        )

    async def search_conversations(self, query: str, *, limit: int, session_id: UUID | None) -> list[ConversationHit]:
        embedding = await self._embed_query(query)
        async with self._sessions() as session:
            hits = await search_chat(
                session,
                embedding,
                chunker_version=CHAT_CHUNKER_VERSION,
                model_key=self._embedder.model_key,
                limit=limit,
                session_id=session_id,
            )
            # The room a hit came from is the console's own binding, not the index's: the index
            # knows sessions, and which room a session served is `claude_chat_sessions.room_id`.
            rooms = dict(
                (
                    await session.execute(
                        select(ClaudeChatSession.session_id, ClaudeChatSession.room_id).where(
                            ClaudeChatSession.session_id.in_({hit.session_id for hit in hits})
                        )
                    )
                ).all()
            )
        return [
            ConversationHit(
                session_id=str(hit.session_id),
                room_id=rooms.get(hit.session_id),
                message_ids=[str(message_id) for message_id in hit.message_ids],
                first_message_at=hit.first_message_at,
                last_message_at=hit.last_message_at,
                snippet=hit.text,
                score=hit.score,
            )
            for hit in hits
        ]

    async def read_note(self, path: str) -> str | None:
        async with self._sessions() as session:
            return await read_indexed_text(
                session, path, chunker_version=CHUNKER_VERSION, model_key=self._embedder.model_key
            )

    async def read_messages(self, message_ids: list[UUID]) -> list[ChatMessage]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(
                    ClaudeChatMessage.message_id,
                    ClaudeChatMessage.session_id,
                    ClaudeChatMessage.role,
                    ClaudeChatMessage.content,
                    ClaudeChatMessage.created_at,
                )
                .where(ClaudeChatMessage.message_id.in_(message_ids))
                # Not the caller's order: these are an excerpt of a conversation, and reading them
                # out of sequence would misrepresent who answered whom.
                .order_by(ClaudeChatMessage.created_at, ClaudeChatMessage.message_id)
            )
        return [
            ChatMessage(
                message_id=str(row.message_id),
                session_id=str(row.session_id),
                role=row.role,
                content=row.content,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def status(self) -> IndexStatus:
        model_key = self._embedder.model_key
        async with self._sessions() as session:
            git_state = await current_git_state(session)
            notes: NotesStatus | None = None
            if git_state is not None:
                summary = await git_index_summary(session, chunker_version=CHUNKER_VERSION, model_key=model_key)
                counts = await chunk_counts(session, Corpus.GIT, chunker_version=CHUNKER_VERSION, model_key=model_key)
                notes = NotesStatus(
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
            notes=notes,
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
