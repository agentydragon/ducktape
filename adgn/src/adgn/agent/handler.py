"""Typed MiniCodex event types and JSONL mapping (co-located with handlers).

Note: This module hosts the strongly-typed event algebra used by handlers and
loggers. It intentionally avoids enums and base-class discrimination; each
event is a distinct Pydantic model. Transcript serialization adds a `kind`
string derived from the concrete type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastmcp.client.client import CallToolResult
from pydantic import BaseModel, Field

from adgn.agent.loop_control import LoopDecision, NoLoopDecision
from adgn.agent.types import ToolCall
from adgn.openai_utils.model import InputTokensDetails, OutputTokensDetails, ReasoningItem


# ---- Ground-truth usage (OpenAI upstream fields only; no derived numbers) ----
class GroundTruthUsage(BaseModel):
    model: str = Field(description="Model name used for the request")
    input_tokens: int | None = Field(None, description="Number of input tokens consumed")
    input_tokens_details: InputTokensDetails | None = Field(None, description="Breakdown of input token usage")
    output_tokens: int | None = Field(None, description="Number of output tokens generated")
    output_tokens_details: OutputTokensDetails | None = Field(None, description="Breakdown of output token usage")

    @property
    def total_tokens(self) -> int | None:
        """Total tokens consumed (input + output)."""
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


# ---- Typed events (no shared runtime base required) ----
class UserText(BaseModel):
    text: str


class AssistantText(BaseModel):
    text: str


class ToolCallOutput(BaseModel):
    call_id: str
    result: CallToolResult


# ----- Generic before-tool-call decision algebra (handler-level, generic) -----


class ContinueDecision(BaseModel):
    """Proceed with normal execution (approve tool call)."""

    action: Literal["continue"] = "continue"


class DenyContinueDecision(BaseModel):
    """Deny the tool call but continue the turn (skip this tool, let agent proceed)."""

    action: Literal["deny_continue"] = "deny_continue"
    reason: str | None = None


class AbortTurnDecision(BaseModel):
    """Deny the tool call and abort the entire turn."""

    action: Literal["abort"] = "abort"
    reason: str | None = None


class Response(BaseModel):
    """One OpenAI responses.create result (non-streaming) with usage.

    Emitted once per model call to avoid duplicating usage across assistant/tool events.
    """

    response_id: str | None = None
    usage: GroundTruthUsage
    model: str | None = None
    created_at: datetime | None = None
    idempotency_key: str | None = None


# Union of all current event types (as a typing alias)
type EventType = UserText | AssistantText | ToolCall | ToolCallOutput | Response | ReasoningItem


# ---- Transcript JSONL serialization ----
KIND_MAP: dict[
    type, Literal["user_text", "assistant_text", "tool_call", "function_call_output", "response", "reasoning"]
] = {
    UserText: "user_text",
    AssistantText: "assistant_text",
    ToolCall: "tool_call",
    ToolCallOutput: "function_call_output",
    Response: "response",
    ReasoningItem: "reasoning",
}


type JsonlRecord = dict[str, Any]


def to_jsonl_record(evt: EventType) -> JsonlRecord:
    data = evt.model_dump(mode="json", exclude_none=True)
    data["kind"] = KIND_MAP[type(evt)]
    return data


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
        """Called after receiving a complete model response with usage stats.

        Default: no-op.
        """
        return

    def on_before_sample(self) -> LoopDecision:
        """Called before each model sampling step to control loop behavior.

        Default: no decision (let other handlers or agent decide).

        Returns:
            LoopDecision: Continue | Abort | NoLoopDecision
        """
        return NoLoopDecision()

    def on_user_text_event(self, evt: UserText) -> None:
        """Called when user text is added to the conversation.

        Default: no-op.
        """
        return

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Called when assistant generates text.

        Default: no-op.
        """
        return

    def on_tool_call_event(self, evt: ToolCall) -> None:
        """Called when the model requests a tool call.

        Default: no-op.
        """
        return

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        """Called when a tool call completes and returns a result.

        Default: no-op.
        """
        return

    def on_reasoning(self, item: ReasoningItem) -> None:
        """Called when the model emits reasoning tokens (extended thinking mode).

        Default: no-op.
        """
        return
