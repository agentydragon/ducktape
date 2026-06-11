from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from agent_core.events import AssistantText, Response, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler
from agent_server.agent_types import AgentID
from agent_server.persist.types import Persistence
from openai_utils.model import ReasoningItem

logger = logging.getLogger(__name__)


class RunPersistenceHandler(BaseHandler):
    """Ever-present handler that appends canonical transcript items to persistence."""

    def __init__(self, *, persistence: Persistence, agent_id: AgentID) -> None:
        self._persistence = persistence
        self._agent_id = agent_id
        self._seq = 0
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro: Any) -> None:
        t: asyncio.Task = asyncio.create_task(coro)
        self._tasks.add(t)

        def _done(task: asyncio.Task) -> None:
            self._tasks.discard(task)
            exc = task.exception()
            if exc:
                logger.exception("persistence task failed", exc_info=exc)

        t.add_done_callback(_done)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    async def drain(self) -> None:
        """Wait for all in-flight persistence tasks to finish.

        Raises RuntimeError if any task failed. Callers can decide whether to
        proceed with destructive actions (like purge) or abort.
        """
        pending: list[asyncio.Task] = list(self._tasks)
        if not pending:
            return
        results = await asyncio.gather(*pending, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            # Summarize unique error types for clarity
            kinds = sorted({type(e).__name__ for e in errors})
            raise RuntimeError(f"persistence_drain_failed: {', '.join(kinds)}")

    def _record_event(
        self, evt: UserText | AssistantText | ToolCall | ToolCallOutput | Response | ReasoningItem
    ) -> None:
        """Common append path: bump seq, enqueue append_event.

        Keeps ordering by incrementing a local sequence.
        Event must have 'type' field for discriminated union.
        """
        self._seq += 1
        self._spawn(self._persistence.append_event(agent_id=self._agent_id, seq=self._seq, ts=self._now(), event=evt))

    # BaseHandler typed hooks --------------------------------------------------
    def on_user_text_event(self, evt: UserText) -> None:
        self._record_event(evt)

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._record_event(evt)

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._record_event(evt)

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        self._record_event(evt)

    def on_reasoning(self, item: ReasoningItem) -> None:
        self._record_event(item)

    def on_response(self, evt: Response) -> None:
        self._record_event(evt)
