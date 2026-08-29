"""Reading the chat corpus out of the console's own tables.

**Prompts and messages, and only completed ones.** Reasoning and tool calls are the session's own
working rather than what was said; an item still open is being written into, and a failed one
records that nothing was said.

A prompt no session has claimed yet has no session to be indexed under, so it is left for the sweep
that runs after it is claimed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.conversation.item_vocabulary import ItemStatus, ItemType
from haku.console.database_schema import ConversationItem
from haku.recall_index.chat_corpus import IndexedMessage, Speaker

_SAID = (ItemType.PROMPT, ItemType.MESSAGE)


@dataclass(frozen=True, slots=True)
class SessionShape:
    """What a session looks like in the source, which is what decides whether to re-index it."""

    session_id: UUID
    conversation_id: UUID
    message_count: int
    last_message_at: datetime.datetime


async def session_shapes(source: AsyncSession) -> list[SessionShape]:
    """Every session with something to index, and the shape the sync compares against.

    One grouped scan rather than a query per session: the common run finds nothing changed, and
    that should cost one round trip for the whole corpus. A session's items all carry its one
    conversation, so grouping by the pair still yields one row per session.
    """
    result = await source.execute(
        select(
            ConversationItem.session_id,
            ConversationItem.conversation_id,
            func.count().label("message_count"),
            func.max(ConversationItem.created_at).label("last_message_at"),
        )
        .where(
            ConversationItem.session_id.isnot(None),
            ConversationItem.item_type.in_(_SAID),
            ConversationItem.status == ItemStatus.COMPLETE,
        )
        .group_by(ConversationItem.session_id, ConversationItem.conversation_id)
    )
    return [SessionShape(**row) for row in result.mappings()]


async def load_messages(source: AsyncSession, session_id: UUID) -> list[IndexedMessage]:
    """One session's completed prompts and messages, in conversation order."""
    result = await source.execute(
        select(
            ConversationItem.item_id,
            ConversationItem.item_type,
            ConversationItem.item_text,
            ConversationItem.created_at,
        )
        .where(
            ConversationItem.session_id == session_id,
            ConversationItem.item_type.in_(_SAID),
            ConversationItem.status == ItemStatus.COMPLETE,
        )
        # `item_id` breaks ties: two rows can share a timestamp, and a window whose contents
        # reshuffle between syncs would re-embed for no reason.
        .order_by(ConversationItem.created_at, ConversationItem.item_id)
    )
    return [
        IndexedMessage(
            message_id=item_id,
            speaker=Speaker.USER if item_type is ItemType.PROMPT else Speaker.ASSISTANT,
            content=text,
            created_at=created_at,
        )
        for item_id, item_type, text, created_at in result
    ]
