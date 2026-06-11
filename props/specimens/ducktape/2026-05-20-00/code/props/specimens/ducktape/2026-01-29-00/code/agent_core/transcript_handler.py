from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pydantic_core

from agent_core.events import ApiRequest, AssistantText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler, Response
from openai_utils.model import ReasoningItem

# Union of all event types we write to transcript
TranscriptEvent = UserText | AssistantText | ToolCall | ToolCallOutput | ReasoningItem | Response | ApiRequest


class TranscriptHandler(BaseHandler):
    """Writes timestamped JSONL event stream to file."""

    def __init__(self, *, events_path: Path) -> None:
        self._path = events_path
        # Fail fast if a transcript already exists at destination
        if self._path.exists():
            raise FileExistsError(f"Transcript already exists: {self._path}")

    def _write_event(self, evt: TranscriptEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rec = evt.model_dump(mode="json", exclude_none=True)
        # Timestamped envelope (events.jsonl)
        out = {"ts": datetime.now(UTC).isoformat(), **rec}
        with self._path.open("a", encoding="utf-8") as f:
            f.write(pydantic_core.to_json(out, fallback=str).decode("utf-8") + "\n")

    # ---- BaseHandler hooks (typed) ----
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
        # Record one responses.create result per model call with usage
        self._write_event(evt)

    def on_api_request_event(self, evt: ApiRequest) -> None:
        # Record the full API request before sending
        self._write_event(evt)
