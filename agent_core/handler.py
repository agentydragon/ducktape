"""Event handlers used by the Agent loop."""

from __future__ import annotations

from agent_core.events import ApiRequest, AssistantText, Response, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.loop_control import InjectItems, LoopDecision, NoAction
from openai_utils.model import ReasoningItem, UserMessage


class BaseHandler:
    """No-op event hooks; keep overrides fast and nonblocking; exceptions propagate."""

    def on_error(self, exc: Exception) -> None:
        """Called on fatal agent errors; re-raises by default."""
        raise exc

    def on_response(self, evt: Response) -> None:
        """Called after receiving a complete model response with usage stats."""
        return

    def on_api_request_event(self, evt: ApiRequest) -> None:
        """Called before sending a request to the OpenAI API."""
        return

    def on_before_sample(self) -> LoopDecision:
        """Return a decision before sampling; the first non-NoAction decision wins."""
        return NoAction()

    def on_system_text_event(self, evt: SystemText) -> None:
        return

    def on_user_text_event(self, evt: UserText) -> None:
        return

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        return

    def on_tool_call_event(self, evt: ToolCall) -> None:
        return

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        """Receive each tool result added to the transcript."""
        return

    def on_reasoning(self, item: ReasoningItem) -> None:
        return

    def on_compaction_complete(self, compacted: bool) -> None:
        """Called after an attempt; compacted is true on success and false if skipped."""
        return


class RedirectOnTextMessageHandler(BaseHandler):
    """Inject a reminder on the next sample after text without a tool call."""

    def __init__(self, reminder_message: str):
        self._reminder = reminder_message
        self._text_detected = False
        self._tool_called = False

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Mark nonempty text; models may emit empty output_text with tool calls."""
        if evt.text.strip():
            self._text_detected = True

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._tool_called = True

    def on_before_sample(self) -> LoopDecision:
        """Inject after text without a tool call; clear both flags on every poll."""
        redirect = self._text_detected and not self._tool_called
        self._text_detected = False
        self._tool_called = False
        if redirect:
            return InjectItems(items=[UserMessage.text(self._reminder)])
        return NoAction()
