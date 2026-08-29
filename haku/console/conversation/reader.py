"""The `haku_conversations` reader: the store's reads, folded to the wire at the MCP seam.

The store speaks items, turns and frames; the item read model is shared with the SPA's projection
(<item_reads.py>, beside the store that produces it), and this adapter is the MCP half: the
settled stream, paged. The other three reads pass through untouched. Its own module because it
stands on both the store and the fold, which the fold itself must not.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from haku.console.conversation.conversation_event import TurnAborted, TurnAnswered, TurnFailed
from haku.console.conversation.item_reads import Item, item_of
from haku.console.conversation.reads import (
    FrameRecord,
    SessionCursor,
    SessionRecord,
    TurnCursor,
    TurnRecord,
    WorkerResult,
    WorkerStatus,
)
from haku.console.conversation_read_access import ConversationReadScope
from haku.console.session.session_frames import SessionFrameKind
from haku.console.session.status import SessionStatus
from haku.console.session.store import Store, WorkerOutcome


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
        kinds: Sequence[SessionFrameKind] | None = None,
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

    async def get_worker_result(self, session_id: UUID, *, scope: ConversationReadScope) -> WorkerResult:
        """A dispatched worker's status and, once it has answered, its final message (#5193)."""
        return _worker_result_of(await self._store.worker_outcome(session_id, scope=scope))


def _worker_result_of(outcome: WorkerOutcome) -> WorkerResult:
    """Coarsen one worker session's lifecycle to the three states a polling orchestrator acts on.

    A `failed` session has died whatever its last turn managed, so the session's own failure surface
    wins over the turn. Otherwise the session's most recent turn decides: an answered turn is `done`
    with that answer even though the session itself stays open between turns (a one-shot worker does
    not close on answering), a failed or aborted turn is `failed`, and a turn still in flight — or a
    session that ended cleanly without one — is `running` until it settles (or `done` once closed).
    """
    if outcome.session_status == SessionStatus.FAILED:
        return WorkerResult(status=WorkerStatus.FAILED, result=outcome.error)
    match outcome.latest_turn_end:
        case TurnAnswered():
            return WorkerResult(status=WorkerStatus.DONE, result=outcome.final_message)
        case TurnFailed(failure=failure):
            return WorkerResult(status=WorkerStatus.FAILED, result=failure)
        case TurnAborted():
            return WorkerResult(status=WorkerStatus.FAILED, result=None)
        case None:
            if outcome.session_status == SessionStatus.CLOSED:
                return WorkerResult(status=WorkerStatus.DONE, result=outcome.final_message)
            return WorkerResult(status=WorkerStatus.RUNNING, result=None)
