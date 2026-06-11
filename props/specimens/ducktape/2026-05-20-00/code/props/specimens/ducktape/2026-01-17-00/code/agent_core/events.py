"""Agent event types.

TODO: Separate "append-only event log" from "current context window items".
Currently these event types serve both purposes:
1. Append-only immutable log (for persistence, replay, analysis)
2. Limited-size context window sent to OpenAI (which gets compacted/cleared)

These are conceptually different: compaction clears the agent's _transcript list
but doesn't "un-happen" the events. Consider splitting into:
- ImmutableEvent types (never deleted, append-only)
- ContextItem types (current working set, can be compacted/pruned)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from mcp import types as mcp_types
from pydantic import BaseModel, Field

from openai_utils.model import InputTokensDetails, OutputTokensDetails, ReasoningItem, ResponsesRequest

__all__ = [
    "REFLECTION_EVENT_TYPES",
    "AgentEvent",
    "ApiRequest",
    "AssistantText",
    "EventType",
    "GroundTruthUsage",
    "ReasoningItem",
    "Response",
    "SystemText",
    "ToolCall",
    "ToolCallOutput",
    "UserText",
]


# ---- Ground-truth usage (OpenAI upstream fields only; no derived numbers) ----
class GroundTruthUsage(BaseModel):
    model: str
    input_tokens: int | None = None
    input_tokens_details: InputTokensDetails | None = None
    output_tokens: int | None = None
    output_tokens_details: OutputTokensDetails | None = None
    total_tokens: int | None = None


# ---- Typed events (discriminated by "type" field) ----
class SystemText(BaseModel):
    type: Literal["system_text"] = "system_text"
    text: str


class UserText(BaseModel):
    type: Literal["user_text"] = "user_text"
    text: str


class AssistantText(BaseModel):
    type: Literal["assistant_text"] = "assistant_text"
    text: str


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    name: str
    args_json: str | None = None
    call_id: str


class ToolCallOutput(BaseModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    result: mcp_types.CallToolResult


class ApiRequest(BaseModel):
    """Full OpenAI API request payload (before sending).

    Captures the complete request including fully-evaluated instructions.
    """

    type: Literal["api_request"] = "api_request"

    # The complete request (reuses existing typed model)
    request: ResponsesRequest

    # TODO: Consider moving model into ResponsesRequest and removing the "model handle" layer
    # Currently model lives on client, not in request object
    model: str

    # Correlation metadata
    request_id: UUID  # Correlate with Response event
    phase_number: int  # Count of Response events so far (which sampling phase)


class Response(BaseModel):
    """One OpenAI responses.create result (non-streaming) with usage.

    Emitted once per model call to avoid duplicating usage across assistant/tool events.
    """

    type: Literal["response"] = "response"
    response_id: str
    request_id: UUID | None = None  # Correlate with ApiRequest
    usage: GroundTruthUsage
    model: str
    created_at: datetime | None = None
    idempotency_key: str | None = None


# Union of all current event types (discriminated by "type" field)
EventType = Annotated[
    SystemText | UserText | AssistantText | ToolCall | ToolCallOutput | ApiRequest | Response | ReasoningItem,
    Field(discriminator="type"),
]

# Alias for cleaner imports
AgentEvent = EventType

# Event types relevant for GEPA reflection - excludes ApiRequest/Response to prevent O(n²) context blowup
REFLECTION_EVENT_TYPES = (ToolCall, ToolCallOutput, AssistantText, ReasoningItem)
