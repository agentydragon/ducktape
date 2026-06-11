"""Handler interface and decision types for Agent agent loop.

This module defines the BaseHandler interface and core handlers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel

from agent_core.events import ApiRequest, AssistantText, Response, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.loop_control import Abort, InjectItems, LoopDecision, NoAction
from openai_utils.model import ReasoningItem, UserMessage

__all__ = [
    "AbortIf",
    "AbortTurnDecision",
    "BaseHandler",
    "CaptureTextHandler",
    "ContinueDecision",
    "FinishOnTextMessageHandler",
    "RedirectOnTextMessageHandler",
    "SequenceHandler",
]


# ----- Generic before-tool-call decision algebra (handler-level, generic) -----


class ContinueDecision(BaseModel):
    """Proceed with normal execution."""

    action: Literal["continue"] = "continue"


class AbortTurnDecision(BaseModel):
    """Request abort of the entire turn."""

    action: Literal["abort"] = "abort"
    reason: str | None = None


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


class SequenceHandler(BaseHandler):
    """Execute a fixed sequence of actions, then NoAction() forever.

    Examples:
        # Inject once
        SequenceHandler([InjectItems(items=[call1, call2])])

        # Inject, sample, then passthrough
        SequenceHandler([InjectItems(...), NoAction()])
    """

    def __init__(self, actions: Sequence[LoopDecision]) -> None:
        self._actions = list(actions)
        self._index = 0

    def on_before_sample(self) -> LoopDecision:
        if self._index >= len(self._actions):
            return NoAction()
        action = self._actions[self._index]
        self._index += 1
        return action


class AbortIf(BaseHandler):
    """Loop controller: abort if condition is met, otherwise continue.

    Pass a should_abort callable that returns True when the agent should stop
    (e.g., submit_state.result is set).

    In the sequential evaluation model, handler ordering matters. Place AbortIf
    after any bootstrap handlers in the handler list to ensure bootstrap completes first.

    Note: The agent's tool_policy (typically RequireAnyTool) is configured at
    construction time and applies throughout the agent's lifetime.
    """

    def __init__(self, should_abort: Callable[[], bool]) -> None:
        self._should_abort = should_abort

    def on_before_sample(self) -> LoopDecision:
        if self._should_abort():
            return Abort()
        return NoAction()


class FinishOnTextMessageHandler(BaseHandler):
    """Abort the agent loop when assistant sends a text message.

    Used for interactive scenarios where each agent.run() should complete
    after the assistant responds with text (allowing user to respond).

    Usage:
        handlers = [FinishOnTextMessageHandler(), ...]
        agent = await Agent.create(..., handlers=handlers, tool_policy=AllowAnyToolOrTextMessage())
        while True:
            user_input = get_user_input()
            agent.process_message(UserMessage.text(user_input))
            await agent.run()  # Returns after assistant sends text
    """

    def __init__(self):
        self._text_detected = False

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Mark that assistant text was detected."""
        self._text_detected = True

    def on_before_sample(self) -> LoopDecision:
        """Abort if assistant sent text in the previous turn."""
        if self._text_detected:
            self._text_detected = False
            return Abort()
        return NoAction()


class CaptureTextHandler(BaseHandler):
    """Capture assistant text and abort loop for conversational sub-agents.

    Unlike FinishOnTextMessageHandler which just aborts, this handler captures
    the text so it can be retrieved after run() completes. Used for sub-agents
    that exchange messages with their parent agent.

    Usage:
        handler = CaptureTextHandler()
        handlers = [handler, ...]
        agent = await Agent.create(..., handlers=handlers, tool_policy=AllowAnyToolOrTextMessage())

        agent.process_message(UserMessage.text("Do something"))
        await agent.run()  # Returns after assistant sends text
        response = handler.take()  # Get captured text, clears state

        agent.process_message(UserMessage.text("Do more"))
        await agent.run()
        response = handler.take()
    """

    def __init__(self) -> None:
        self._captured: str | None = None
        self._text_detected = False

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Capture assistant text and mark for abort."""
        self._captured = evt.text
        self._text_detected = True

    def on_before_sample(self) -> LoopDecision:
        """Abort if assistant sent text in the previous turn."""
        if self._text_detected:
            self._text_detected = False
            return Abort()
        return NoAction()

    def take(self) -> str:
        """Return captured text and clear state for next run.

        Returns:
            The captured assistant text.

        Raises:
            ValueError: If no text was captured (agent may have hit max turns
                or aborted for another reason).
        """
        if self._captured is None:
            raise ValueError("No text captured (agent may have exited before producing text)")
        text = self._captured
        self._captured = None
        return text

    @property
    def has_text(self) -> bool:
        """Check if text was captured without consuming it."""
        return self._captured is not None


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

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Mark that assistant text was detected."""
        self._text_detected = True

    def on_before_sample(self) -> LoopDecision:
        """If text was detected last turn, inject reminder and reset flag."""
        if self._text_detected:
            self._text_detected = False
            return InjectItems(items=[UserMessage.text(self._reminder)])
        return NoAction()
