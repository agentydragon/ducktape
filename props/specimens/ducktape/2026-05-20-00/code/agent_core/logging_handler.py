"""Handler that logs agent loop events to a standard library logger."""

from __future__ import annotations

import logging

from agent_core.events import AssistantText, Response, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler
from agent_core.loop_control import LoopDecision, NoAction
from openai_utils.model import ReasoningItem


class LoggingHandler(BaseHandler):
    """Log agent loop events at DEBUG level."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def on_before_sample(self) -> LoopDecision:
        self._logger.debug("Sampling model...")
        return NoAction()

    def on_system_text_event(self, evt: SystemText) -> None:
        self._logger.debug("System: %s", _trunc(evt.text))

    def on_user_text_event(self, evt: UserText) -> None:
        self._logger.debug("User: %s", _trunc(evt.text))

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._logger.debug("Assistant: %s", _trunc(evt.text))

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._logger.debug("Tool call: %s(%s)", evt.name, _trunc(evt.args_json or ""))

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        self._logger.debug("Tool result [%s]: %s", evt.call_id, _trunc(str(evt.result)))

    def on_response(self, evt: Response) -> None:
        self._logger.debug("Response %s: model=%s tokens=%s", evt.response_id, evt.model, evt.usage.total_tokens)

    def on_reasoning(self, item: ReasoningItem) -> None:
        summary_text = " ".join(s.text for s in item.summary) if item.summary else "(no summary)"
        self._logger.debug("Reasoning: %s", _trunc(summary_text))

    def on_error(self, exc: Exception) -> None:
        self._logger.error("Agent error: %s", exc)
        raise exc


def _trunc(s: str, maxlen: int = 200) -> str:
    return s if len(s) <= maxlen else s[:maxlen] + "..."
