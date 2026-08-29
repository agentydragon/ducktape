"""The finished conversation tail handed to a replacement session.

This is conversation state, not channel history: prompts and answers are read from the console's
own materialized items so every attached surface resumes from the same account of what was said.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conversation.item_vocabulary import ItemStatus, ItemType
from haku.console.database_schema import ConversationItem, Session


@dataclass(frozen=True)
class RecordedMessage:
    """One finished prompt or answer, before the prompt renderer assigns a speaker label."""

    item_type: ItemType
    body: str
    sent_at: datetime


class ConversationHistory:
    """The finished conversation tail shared by every attached chat surface."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def earlier_sessions(self, conversation_id: UUID, *, before_session: UUID) -> tuple[UUID, ...]:
        """The conversation's other sessions, oldest first, for the prompt's prior-session ids."""
        listed = (
            select(Session.session_id)
            .where(Session.conversation_id == conversation_id, Session.session_id != before_session)
            .order_by(Session.created_at, Session.session_id)
        )
        async with self._sessions() as db:
            return tuple((await db.scalars(listed)).all())

    async def recent(self, conversation_id: UUID, *, before_session: UUID, limit: int) -> tuple[RecordedMessage, ...]:
        """The last *limit* spoken items, oldest first, excluding *before_session*'s queued prompt."""
        said = (
            select(ConversationItem.item_type, ConversationItem.item_text, ConversationItem.created_at)
            .where(
                ConversationItem.conversation_id == conversation_id,
                ConversationItem.session_id.is_distinct_from(before_session),
                ConversationItem.item_type.in_((ItemType.PROMPT, ItemType.MESSAGE)),
                ConversationItem.status == ItemStatus.COMPLETE,
                func.trim(ConversationItem.item_text) != "",
            )
            .order_by(ConversationItem.created_at.desc(), ConversationItem.item_id.desc())
            .limit(limit)
        )
        async with self._sessions() as db:
            rows = (await db.execute(said)).all()
        return tuple(
            RecordedMessage(item_type=item_type, body=text, sent_at=created_at)
            for item_type, text, created_at in reversed(rows)
        )
