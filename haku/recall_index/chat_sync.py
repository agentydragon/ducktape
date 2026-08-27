"""Bring one configured chat index up to what the console has recorded.

The unit of work is a window of messages; the unit of skipping is a session. A session whose
message count and newest message are unchanged under the same regime is never read, so a run
over an unchanged source costs two queries and no embedding.

Unlike the git sync there is no repository to fetch and no mirror to keep: the index lives in
the console's own database, which is where the messages already are, so the source is one query
away and the whole sync lands in the caller's transaction.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from haku.recall_index.chat_corpus import chat_chunker_key, chunk_messages
from haku.recall_index.chat_source import SessionShape, load_messages, session_shapes
from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.schema import ChatSessionState
from haku.recall_index.store import (
    ContentRow,
    chat_session_states,
    forget_chat_sessions,
    insert_contents,
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
    # Changed, but still being written to — materialized by a later sweep, not skipped.
    sessions_settling: int
    sessions_forgotten: int
    windows_written: int
    contents_materialized: int


def is_indexed(
    state: ChatSessionState | None, shape: SessionShape, *, budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
) -> bool:
    """Whether a session's indexed form still matches the source, under this regime.

    Public because `index_status` answers "how far behind is the corpus" with exactly this
    question, and two spellings of it would let the report disagree with what a sweep does.
    """
    return state is not None and (state.message_count, state.last_message_at, state.chunker_key) == (
        shape.message_count,
        shape.last_message_at,
        chat_chunker_key(budget),
    )


async def sync_chat(
    session: AsyncSession,
    *,
    index_id: str,
    now: datetime.datetime,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
    quiet_for: datetime.timedelta = DEFAULT_QUIET_PERIOD,
) -> ChatSyncReport:
    """Materialize every changed quiet chat session; embedding is a shared later stage."""
    regime = chat_chunker_key(budget)
    shapes = await session_shapes(session)
    states = await chat_session_states(session, index_id)

    # A session the console has dropped would otherwise stay searchable forever: chat windows are
    # reachable until deleted, where a git blob stops being reachable when it leaves the tip.
    forgotten = sorted(set(states) - {shape.session_id for shape in shapes})
    await forget_chat_sessions(session, forgotten, index_id=index_id)

    indexed = 0
    unchanged = 0
    settling = 0
    windows_written = 0
    contents_materialized = 0
    for shape in shapes:
        state = states.get(shape.session_id)
        if is_indexed(state, shape, budget=budget):
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
        # queues it once, and so does a session that repeats another session's exchange.
        by_content = {chunk.content_sha: chunk for chunk in chunks}
        await insert_contents(
            session, (ContentRow(content_sha=chunk.content_sha, content=chunk.text) for chunk in by_content.values())
        )
        await replace_chat_session(
            session,
            shape.session_id,
            chunks,
            index_id=index_id,
            conversation_id=shape.conversation_id,
            message_count=shape.message_count,
            last_message_at=shape.last_message_at,
            chunker_key=regime,
            now=now,
        )
        indexed += 1
        windows_written += len(chunks)
        contents_materialized += len(by_content)

    report = ChatSyncReport(
        sessions_indexed=indexed,
        sessions_unchanged=unchanged,
        sessions_settling=settling,
        sessions_forgotten=len(forgotten),
        windows_written=windows_written,
        contents_materialized=contents_materialized,
    )
    logger.info("synced chat index: %s", report)
    return report
