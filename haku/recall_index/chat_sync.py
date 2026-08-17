"""Bring the chat corpus up to what the console has recorded.

The unit of work is a window of messages; the unit of skipping is a session. A session whose
message count and newest message are unchanged under the same regime is never read, so a run
over an unchanged corpus costs two queries and no embedding.

Unlike the git sync there is no repository to fetch and no mirror to keep: the index lives in
the console's own database, which is where the messages already are, so the corpus is one query
away and the whole sync lands in the caller's transaction.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from more_itertools import batched
from sqlalchemy.ext.asyncio import AsyncSession

from haku.recall_index.chat_corpus import chat_chunker_key, chunk_messages
from haku.recall_index.chat_source import SessionShape, load_messages, session_shapes
from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.embedder import EMBED_BATCH, Embedder
from haku.recall_index.schema import ChatSessionState
from haku.recall_index.store import (
    ContentEmbeddingRow,
    chat_session_states,
    embedded_content,
    forget_chat_sessions,
    insert_content_embeddings,
    replace_chat_session,
)

logger = logging.getLogger(__name__)

# How long a session must go without a new message before it is worth indexing. A changed session
# is re-windowed wholesale, so indexing one mid-exchange re-chunks its whole tail and the next turn
# does it again. Shorter than any sane sweep interval, so a finished conversation is still picked up
# by the following sweep.
DEFAULT_QUIET_PERIOD = datetime.timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class ChatSyncReport:
    sessions_indexed: int
    sessions_unchanged: int
    # Changed, but still being written to — indexed by a later sweep, not skipped.
    sessions_settling: int
    sessions_forgotten: int
    windows_written: int
    windows_embedded: int
    windows_reused: int


def is_indexed(
    state: ChatSessionState | None, shape: SessionShape, *, model_key: str, budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
) -> bool:
    """Whether a session's indexed form still matches the source, under this regime.

    Public because `index_status` answers "how far behind is the corpus" with exactly this
    question, and two spellings of it would let the report disagree with what a sweep does.
    """
    return state is not None and (state.message_count, state.last_message_at, state.chunker_key, state.model_key) == (
        shape.message_count,
        shape.last_message_at,
        chat_chunker_key(budget),
        model_key,
    )


async def sync_chat(
    session: AsyncSession,
    *,
    embedder: Embedder,
    now: datetime.datetime,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
    quiet_for: datetime.timedelta = DEFAULT_QUIET_PERIOD,
) -> ChatSyncReport:
    """Index every chat session that has changed since it was last indexed and has gone quiet."""
    regime = chat_chunker_key(budget)
    shapes = await session_shapes(session)
    states = await chat_session_states(session)

    # A session the console has dropped would otherwise stay searchable forever: chat windows are
    # reachable until deleted, where a git blob stops being reachable when it leaves the tip.
    forgotten = sorted(set(states) - {shape.session_id for shape in shapes})
    await forget_chat_sessions(session, forgotten)

    indexed = 0
    unchanged = 0
    settling = 0
    windows_written = 0
    windows_embedded = 0
    windows_reused = 0
    for shape in shapes:
        state = states.get(shape.session_id)
        if is_indexed(state, shape, model_key=embedder.model_key, budget=budget):
            unchanged += 1
            continue

        # A session someone is still talking in is left alone: its trailing window changes shape
        # with every turn. Nothing is lost — the shape it was skipped at is not recorded, so the
        # next sweep still sees it as changed.
        if shape.last_message_at > now - quiet_for:
            settling += 1
            continue

        chunks = chunk_messages(await load_messages(session, shape.session_id), budget)
        # Distinct content, not distinct windows: a session that says the same thing twice
        # embeds it once, and so does a session that repeats another session's exchange.
        by_content = {chunk.content_sha: chunk for chunk in chunks}
        already_embedded = await embedded_content(session, by_content, model_key=embedder.model_key)
        pending = [chunk for content_sha, chunk in sorted(by_content.items()) if content_sha not in already_embedded]

        for batch in batched(pending, EMBED_BATCH):
            vectors = await embedder.embed_documents([chunk.text for chunk in batch])
            await insert_content_embeddings(
                session,
                [
                    ContentEmbeddingRow(
                        content_sha=chunk.content_sha,
                        content=chunk.text,
                        model_key=embedder.model_key,
                        embedding=vector,
                    )
                    for chunk, vector in zip(batch, vectors, strict=True)
                ],
            )
        await replace_chat_session(
            session,
            shape.session_id,
            chunks,
            message_count=shape.message_count,
            last_message_at=shape.last_message_at,
            chunker_key=regime,
            model_key=embedder.model_key,
            now=now,
        )
        indexed += 1
        windows_written += len(chunks)
        windows_embedded += len(pending)
        windows_reused += len(already_embedded)

    report = ChatSyncReport(
        sessions_indexed=indexed,
        sessions_unchanged=unchanged,
        sessions_settling=settling,
        sessions_forgotten=len(forgotten),
        windows_written=windows_written,
        windows_embedded=windows_embedded,
        windows_reused=windows_reused,
    )
    logger.info("synced chat index: %s", report)
    return report
