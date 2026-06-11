"""Database event handler for Agent runs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from agent_core.events import ApiRequest, AssistantText, Response, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler
from openai_utils.model import ReasoningItem
from props.core.db.models import Event
from props.core.db.session import get_session

logger = logging.getLogger(__name__)


class DatabaseEventHandler(BaseHandler):
    """Database event writer for Agent runs.

    Writes events to the database events table, maintaining sequence order.
    Each event is linked to the agent run via agent_run_id.

    Usage:
        from uuid import UUID
        handler = DatabaseEventHandler(agent_run_id=UUID('...'))
        Agent.create(..., handlers=[handler, ...])
    """

    def __init__(self, *, agent_run_id: UUID) -> None:
        """Initialize handler for a specific agent run.

        Args:
            agent_run_id: UUID linking this event stream to the agent run
        """
        self.agent_run_id = agent_run_id
        self._sequence_num = 0

    def _write_event(
        self,
        evt: SystemText | UserText | AssistantText | ToolCall | ToolCallOutput | Response | ReasoningItem | ApiRequest,
    ) -> None:
        """Write event to database with sequence number."""
        event_type = evt.type

        with get_session() as session:
            session.add(
                Event(
                    agent_run_id=self.agent_run_id,
                    sequence_num=self._sequence_num,
                    event_type=event_type,
                    timestamp=datetime.now(UTC),
                    payload=evt,
                )
            )
            session.flush()

        self._sequence_num += 1
        logger.debug(f"Wrote event to DB: {self.agent_run_id=} {self._sequence_num - 1=} {event_type=}")

    # ---- BaseHandler hooks (typed) ----
    def on_system_text_event(self, evt: SystemText) -> None:
        self._write_event(evt)

    def on_user_text_event(self, evt: UserText) -> None:
        self._write_event(evt)

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._write_event(evt)

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._write_event(evt)

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        self._write_event(evt)

    def on_reasoning(self, item: ReasoningItem) -> None:
        self._write_event(item)

    def on_response(self, evt: Response) -> None:
        self._write_event(evt)

    def on_api_request_event(self, evt: ApiRequest) -> None:
        self._write_event(evt)
