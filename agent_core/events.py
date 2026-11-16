"""Agent event types."""

from typing import Literal

from pydantic import BaseModel

from agent_core.tool_provider import ToolResult


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
    result: ToolResult
