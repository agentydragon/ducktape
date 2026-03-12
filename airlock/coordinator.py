"""Shared coordination state between the agent MCP face and operator REST face.

Holds the pending decision futures (created by agent tool calls, resolved by
operator approve/reject) and SSE subscriber queues (consumed by operator frontend).
Both AirlockServer (MCP) and the operator REST API operate on the same coordinator.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from airlock.models import ActionKey, OperatorDecision, PendingState
from airlock.storage import ActionStorage

logger = logging.getLogger(__name__)


class ActionCoordinator:
    """Coordinates pending decisions and SSE events between agent and operator faces.

    Constructed before the server starts. Storage is set during lifespan init
    (handlers only run after lifespan, so the lazy access is safe).
    """

    def __init__(self) -> None:
        self._storage: ActionStorage | None = None
        self._pending: dict[ActionKey, asyncio.Future[OperatorDecision]] = {}
        self._sse_subscribers: set[asyncio.Queue[dict[str, object]]] = set()

    def set_storage(self, storage: ActionStorage) -> None:
        self._storage = storage

    async def close(self) -> None:
        if self._storage is not None:
            await self._storage.close()

    @property
    def storage(self) -> ActionStorage:
        if self._storage is None:
            raise RuntimeError("storage not initialised — server not started")
        return self._storage

    # ── Pending decisions ──────────────────────────────────────────────────────

    def register_pending(self, key: ActionKey) -> asyncio.Future[OperatorDecision]:
        """Create and register a future for a pending human decision."""
        fut: asyncio.Future[OperatorDecision] = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        return fut

    def remove_pending(self, key: ActionKey) -> asyncio.Future[OperatorDecision] | None:
        """Remove and return the pending future, if any."""
        return self._pending.pop(key, None)

    async def decide(self, key: ActionKey, decision: OperatorDecision) -> None:
        """Resolve a pending decision. Raises ValueError if not decidable."""
        action = await self.storage.get_action(key)
        if action is None:
            raise ValueError(f"Action not found: {key.session_key}/{key.action_seq}")
        if not isinstance(action.state, PendingState):
            raise ValueError(f"Action {key.session_key}/{key.action_seq} is not pending ({action.state.status=})")
        fut = self._pending.get(key)
        if fut is None or fut.done():
            raise ValueError(f"Action {key.session_key}/{key.action_seq} is not awaiting a human decision")
        fut.set_result(decision)

    # ── SSE events ─────────────────────────────────────────────────────────────

    def subscribe_sse(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._sse_subscribers.add(queue)
        return queue

    def unsubscribe_sse(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        self._sse_subscribers.discard(queue)

    def push_sse_event(self, event: dict[str, object]) -> None:
        for queue in self._sse_subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
