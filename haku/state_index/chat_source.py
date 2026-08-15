"""Reading the chat corpus out of the console's own tables.

Only `complete` messages are indexed: a `pending` or `streaming` row is still being written
into, and a `failed` one records that nothing was said.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import ChatMessageStatus
from haku.console.database_schema import SessionMessage
from haku.state_index.chat_corpus import IndexedMessage


@dataclass(frozen=True, slots=True)
class SessionShape:
    """What a session looks like in the source, which is what decides whether to re-index it."""

    session_id: UUID
    message_count: int
    last_message_at: datetime.datetime


async def session_shapes(source: AsyncSession) -> list[SessionShape]:
    """Every session with something to index, and the shape the sync compares against.

    One grouped scan rather than a query per session: the common run finds nothing changed, and
    that should cost one round trip for the whole corpus.
    """
    result = await source.execute(
        select(
            SessionMessage.session_id,
            func.count().label("message_count"),
            func.max(SessionMessage.created_at).label("last_message_at"),
        )
        .where(SessionMessage.status == ChatMessageStatus.COMPLETE)
        .group_by(SessionMessage.session_id)
    )
    return [SessionShape(**row) for row in result.mappings()]


async def load_messages(source: AsyncSession, session_id: UUID) -> list[IndexedMessage]:
    """One session's complete messages, in conversation order."""
    result = await source.execute(
        select(SessionMessage.message_id, SessionMessage.role, SessionMessage.content, SessionMessage.created_at)
        .where(SessionMessage.session_id == session_id)
        .where(SessionMessage.status == ChatMessageStatus.COMPLETE)
        # `message_id` breaks ties: two rows can share a timestamp, and a window whose contents
        # reshuffle between syncs would re-embed for no reason.
        .order_by(SessionMessage.created_at, SessionMessage.message_id)
    )
    return [IndexedMessage(**row) for row in result.mappings()]
