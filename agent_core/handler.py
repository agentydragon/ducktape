"""Handler interface and core handlers for the Agent loop.

This module defines the BaseHandler interface and core handlers.
"""

from __future__ import annotations

from agent_core.events import ApiRequest, AssistantText, Response, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.loop_control import InjectItems, LoopDecision, NoAction
from openai_utils.model import ReasoningItem, UserMessage


class BaseHandler:
    """Base class for agent event handlers with no-op default implementations.

    Subclasses override specific methods to react to agent loop events. All methods
    have sensible defaults so handlers only implement what they need.

    **Contract**:
    - Implementations MUST be fast and non-blocking (use async/await for I/O)
    - Exceptions propagate to the agent loop (fail-fast by default)
    - Return values from hooks influence loop control (e.g., on_before_sample)

    **Common use cases**:
    - Display progress/events to UI (on_user_text_event, on_assistant_text_event)
    - Log transcripts (on_tool_call_event, on_tool_result_event)
    - Control sampling behavior (on_before_sample returns LoopDecision)
    - Persist agent state (on_response)
    """

    def on_error(self, exc: Exception) -> None:
        """Called when the agent encounters a fatal error.

        Default: re-raise exception (fail-fast). Override to log or suppress errors.
        """
        raise exc

    def on_response(self, evt: Response) -> None:
        """Called after receiving a complete model response with usage stats."""
        return

    def on_api_request_event(self, evt: ApiRequest) -> None:
        """Called before sending a request to the OpenAI API."""
        return

    def on_before_sample(self) -> LoopDecision:
        """Called before each model sampling step to control loop behavior.

        Default: no decision (let other handlers or agent decide).

        Returns:
            LoopDecision: NoAction | InjectItems | Abort | Compact
        """
        return NoAction()

    def on_system_text_event(self, evt: SystemText) -> None:
        """Called when a system message is added to the conversation."""
        return

    def on_user_text_event(self, evt: UserText) -> None:
        """Called when user text is added to the conversation."""
        return

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Called when assistant generates text."""
        return

    def on_tool_call_event(self, evt: ToolCall) -> None:
        """Called when the model requests a tool call."""
        return

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        """Called when a tool call completes and returns a result."""
        return

    def on_reasoning(self, item: ReasoningItem) -> None:
        """Called when the model emits reasoning tokens (extended thinking mode)."""
        return

    def on_compaction_complete(self, compacted: bool) -> None:
        """Called after compaction attempt completes.

        Args:
            compacted: True if compaction succeeded, False if skipped
        """
        return


class RedirectOnTextMessageHandler(BaseHandler):
    """Redirect agent when it sends text messages instead of using tools.

    For non-interactive agents that should use MCP tools rather than sending
    conversational text, this handler injects a reminder message when text is
    detected.

    Usage:
        reminder = "You are not interactive. Use tools: foo, bar, baz."
        handlers = [RedirectOnTextMessageHandler(reminder), ...]
        agent = await Agent.create(..., handlers=handlers, tool_policy=AllowAnyToolOrTextMessage())
    """

    def __init__(self, reminder_message: str):
        """Initialize redirect handler.

        Args:
            reminder_message: Message to inject when assistant sends text instead of using tools
        """
        self._reminder = reminder_message
        self._text_detected = False
        self._tool_called = False

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Mark non-empty assistant text. Empty output_text items (some models emit
        one on every turn alongside their tool call) are not "text output"."""
        if evt.text.strip():
            self._text_detected = True

    def on_tool_call_event(self, evt: ToolCall) -> None:
        """Mark that a tool was called this turn."""
        self._tool_called = True

    def on_before_sample(self) -> LoopDecision:
        """Inject the reminder only when the previous turn produced text *instead of*
        tools — i.e. non-empty text with no accompanying tool call."""
        redirect = self._text_detected and not self._tool_called
        self._text_detected = False
        self._tool_called = False
        if redirect:
            return InjectItems(items=[UserMessage.text(self._reminder)])
        return NoAction()
