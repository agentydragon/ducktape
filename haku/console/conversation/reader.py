"""The `haku_conversations` reader: the store's reads, folded to the wire at the MCP seam.

The store speaks items, turns and frames; the item read model is shared with the SPA's projection
(<item_reads.py>, beside the store that produces it), and this adapter is the MCP half: the
settled stream, paged. The other three reads pass through untouched. Its own module because it
stands on both the store and the fold, which the fold itself must not.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from haku.console.conversation.item_reads import Item, item_of
from haku.console.conversation.reads import FrameRecord, SessionCursor, SessionRecord, TurnCursor, TurnRecord
from haku.console.conversation_read_access import ConversationReadScope
from haku.console.session.session_frames import BridgeFrameKind
from haku.console.session.store import Store


class ConversationReads:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def list_sessions(
        self, *, cursor: SessionCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[SessionRecord]:
        return await self._store.list_sessions(cursor=cursor, limit=limit, scope=scope)

    async def read_session_frames(
        self,
        session_id: UUID,
        *,
        cursor: int | None,
        limit: int,
        scope: ConversationReadScope,
        kinds: Sequence[BridgeFrameKind] | None = None,
    ) -> list[FrameRecord]:
        return await self._store.read_session_frames(session_id, cursor=cursor, limit=limit, scope=scope, kinds=kinds)

    async def list_turns(
        self, session_id: UUID, *, cursor: TurnCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[TurnRecord]:
        return await self._store.list_turns(session_id, cursor=cursor, limit=limit, scope=scope)

    async def read_conversation_items(
        self, conversation_id: UUID, *, cursor: int | None, limit: int, scope: ConversationReadScope
    ) -> list[Item]:
        rows = await self._store.read_item_rows(conversation_id, after_seq=cursor, limit=limit, scope=scope)
        return [item_of(row) for row in rows]
