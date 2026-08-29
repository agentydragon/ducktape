"""Concurrent appends to one conversation, against a real Postgres.

`writer_for` takes the conversation row `FOR UPDATE` before allocating a position, so same-conversation
writers serialize on one lock in one order rather than racing the dense `next_event_seq` counter.
This drives several real transactions at one conversation at once and holds the observable contract:
every append lands, and the positions are unique, gapless and strictly increasing — the guarantee a
position-based channel resume depends on. A writer path that appended without that lock would let two
transactions allocate the same position and this would fail.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conversation import log
from haku.console.conversation.conversation_event import SetupNarration
from haku.console.database_schema import Conversation, ConversationEventRow


async def _seed_conversation(sessions: async_sessionmaker[AsyncSession], operator_id: UUID) -> UUID:
    conversation_id = uuid4()
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO conversation (conversation_id, operator_id, harness_kind, next_event_seq, created_at) "
                "VALUES (:c, :o, 'claude_code', 1, :n)"
            ),
            {"c": conversation_id, "o": operator_id, "n": datetime.now(UTC)},
        )
    return conversation_id


async def _append(sessions: async_sessionmaker[AsyncSession], conversation_id: UUID) -> None:
    async with sessions.begin() as db:
        writer = await log.writer_for(db, conversation_id, session_id=None, turn_id=None, now=datetime.now(UTC))
        writer.authored(SetupNarration(text="a setup line"))


async def test_concurrent_appends_get_unique_monotonic_positions(
    migrated_sessions: async_sessionmaker[AsyncSession], operator_id: UUID
) -> None:
    conversation_id = await _seed_conversation(migrated_sessions, operator_id)
    writers = 12

    await asyncio.gather(*[_append(migrated_sessions, conversation_id) for _ in range(writers)])

    async with migrated_sessions() as db:
        seqs = list(
            await db.scalars(
                select(ConversationEventRow.event_seq)
                .where(ConversationEventRow.conversation_id == conversation_id)
                .order_by(ConversationEventRow.event_seq)
            )
        )
        counter = await db.scalar(
            select(Conversation.next_event_seq).where(Conversation.conversation_id == conversation_id)
        )

    # Every writer landed exactly one event, at a gapless strictly-increasing position from the seed,
    # and the counter advanced by exactly that many — no two writers shared a position.
    assert seqs == list(range(1, writers + 1))
    assert counter == writers + 1


if __name__ == "__main__":
    pytest_bazel.main()
