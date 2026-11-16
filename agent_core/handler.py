"""Event handlers used by the Agent loop."""

from __future__ import annotations

from agent_core.events import ApiRequest, AssistantText, Response, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.loop_control import LoopDecision, NoAction
from openai_utils.model import ReasoningItem


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
