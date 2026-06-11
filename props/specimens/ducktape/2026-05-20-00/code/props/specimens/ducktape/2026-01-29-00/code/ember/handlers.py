"""Ember-specific handlers for agent_core.Agent.

These handlers provide ember's custom behaviors:
- EmberSleepHandler: Aborts agent loop when sleep_until_user_message tool signals
- EmberPersistenceHandler: Persists conversation events to JSONL file
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from agent_core.events import AgentEvent, AssistantText, Response, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler
from agent_core.loop_control import Abort, LoopDecision, NoAction
from openai_utils.model import ReasoningItem


class EmberHistoryRecord(BaseModel):
    """Record for persisting agent events to JSONL."""

    timestamp: datetime
    event: AgentEvent

    model_config = ConfigDict(frozen=True, extra="forbid")


_RECORD_ADAPTER: TypeAdapter[EmberHistoryRecord] = TypeAdapter(EmberHistoryRecord)


class EmberSleepHandler(BaseHandler):
    """Aborts agent loop when sleep_until_user_message tool signals.

    The MCP tool calls `request_sleep()` when successfully invoked, which
    sets a flag that causes `on_before_sample()` to return Abort() on the
    next iteration.

    Usage:
        sleep_handler = EmberSleepHandler()
        # Pass sleep_handler.request_sleep as callback to MCP server
        mcp_server = create_ember_tools_server(
            sleep_callback=sleep_handler.request_sleep,
            ...
        )
    """

    def __init__(self) -> None:
        self._should_sleep = False

    def request_sleep(self) -> None:
        """Called by sleep_until_user_message tool to request loop abort."""
        self._should_sleep = True

    def on_before_sample(self) -> LoopDecision:
        """Abort if sleep was requested, otherwise continue."""
        if self._should_sleep:
            self._should_sleep = False
            return Abort()
        return NoAction()

    def reset(self) -> None:
        """Reset sleep state for new conversation turn."""
        self._should_sleep = False


class EmberPersistenceHandler(BaseHandler):
    """Persists conversation events to JSONL file.

    Events are written atomically (write to temp file, then rename) to
    prevent corruption from crashes.
    """

    def __init__(self, history_path: Path) -> None:
        self._path = history_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records: list[EmberHistoryRecord] = []

    def _append_event(self, event: AgentEvent) -> None:
        """Append event and persist to disk."""
        record = EmberHistoryRecord(timestamp=datetime.now(UTC), event=event)
        self._records.append(record)
        self._persist()

    def _persist(self) -> None:
        """Write all records atomically to JSONL file."""
        tmp_path = self._path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(record.model_dump_json())
                handle.write("\n")
        tmp_path.replace(self._path)

    def on_user_text_event(self, evt: UserText) -> None:
        """Persist user text event."""
        self._append_event(evt)

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Persist assistant text event."""
        self._append_event(evt)

    def on_tool_call_event(self, evt: ToolCall) -> None:
        """Persist tool call event."""
        self._append_event(evt)

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        """Persist tool result event."""
        self._append_event(evt)

    def on_response(self, evt: Response) -> None:
        """Persist response event (includes usage stats)."""
        self._append_event(evt)

    def on_reasoning(self, item: ReasoningItem) -> None:
        """Persist reasoning event."""
        self._append_event(item)
