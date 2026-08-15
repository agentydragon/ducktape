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

from haku.state_index.chat_corpus import MessageChunk, chat_chunker_key, chunk_messages
from haku.state_index.chat_source import load_messages, session_shapes
from haku.state_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.state_index.embedder import Embedder
from haku.state_index.schema import Corpus
from haku.state_index.store import (
    ChunkRow,
    cached_content,
    chat_session_states,
    forget_chat_sessions,
    insert_chunks,
    replace_chat_session,
    touch_content,
)

logger = logging.getLogger(__name__)

_EMBED_BATCH = 32


# How long a session must go without a new message before it is worth indexing. A changed
# session is re-windowed wholesale, so indexing one mid-exchange re-chunks its whole tail and
# the next turn does it again; waiting for a pause turns a burst into one pass. Shorter than any
# sane sweep interval, so a finished conversation is still picked up by the following sweep.
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

    # A session the console has dropped would otherwise stay searchable forever: chat windows
    # are reachable until deleted, where a git blob stops being reachable the moment it leaves
    # the tip. This sweep is that difference.
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
        if state is not None and (state.message_count, state.last_message_at, state.chunker_key, state.model_key) == (
            shape.message_count,
            shape.last_message_at,
            regime,
            embedder.model_key,
        ):
            unchanged += 1
            continue

        # A session someone is still talking in is left alone: its trailing window changes shape
        # with every turn, and re-windowing it now only means re-windowing it again next sweep.
        # Nothing is lost — the shape it was skipped at is not recorded, so the next sweep sees
        # it as changed still.
        if shape.last_message_at > now - quiet_for:
            settling += 1
            continue

        chunks = chunk_messages(await load_messages(session, shape.session_id), budget)
        # Distinct content, not distinct windows: a session that says the same thing twice
        # embeds it once, and so does a session that repeats another session's exchange.
        by_content = {chunk.content_sha: chunk for chunk in chunks}
        cached = await cached_content(
            session, Corpus.CHAT, sorted(by_content), chunker_key=regime, model_key=embedder.model_key
        )
        pending = [chunk for content_sha, chunk in sorted(by_content.items()) if content_sha not in cached]

        rows: list[ChunkRow] = []
        for batch in batched(pending, _EMBED_BATCH):
            vectors = await embedder.embed_documents([chunk.text for chunk in batch])
            rows.extend(
                _chunk_row(chunk, vector, chunker_key=regime, model_key=embedder.model_key)
                for chunk, vector in zip(batch, vectors, strict=True)
            )
        await insert_chunks(session, rows, now=now)
        await touch_content(
            session, Corpus.CHAT, sorted(cached), chunker_key=regime, model_key=embedder.model_key, now=now
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
        windows_reused += len(cached)

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


def _chunk_row(chunk: MessageChunk, embedding: list[float], *, chunker_key: str, model_key: str) -> ChunkRow:
    """One embedded window, addressed by its own text — so the span covers all of it."""
    return ChunkRow(
        corpus=Corpus.CHAT,
        content_sha=chunk.content_sha,
        chunk_no=0,
        chunker_key=chunker_key,
        model_key=model_key,
        byte_start=0,
        byte_end=len(chunk.text.encode()),
        text=chunk.text,
        embedding=embedding,
    )
