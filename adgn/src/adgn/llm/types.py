"""Provider-agnostic LLM types.

These types are designed to work across different LLM providers (OpenAI, Anthropic, etc.)
without being tied to any specific provider's API format. Each provider implementation
translates between these types and their native API formats.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


# ------------------------------
# Messages
# ------------------------------


class TextContent(BaseModel):
    """Text content part."""
    type: Literal["text"] = "text"
    text: str


# Future: ImageContent, etc.
ContentPart = TextContent


class Message(BaseModel):
    """Provider-agnostic message.

    Represents a single message in a conversation. Content can be either a simple
    string or a list of content parts (for multimodal support).
    """
    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role="assistant", content=content)


# ------------------------------
# Tools
# ------------------------------


class Tool(BaseModel):
    """Provider-agnostic tool definition.

    Defines a function/tool that the model can call. The parameters field
    should be a JSON Schema object describing the tool's parameters.
    """
    name: str
    description: str
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


class ToolCall(BaseModel):
    """Provider-agnostic tool call from the model."""
    id: str  # Unique ID for this tool call
    name: str  # Tool name
    arguments: dict[str, Any]  # Parsed arguments (not JSON string)


class ToolResult(BaseModel):
    """Provider-agnostic tool result to send back to the model."""
    tool_call_id: str  # References the ToolCall.id
    content: str  # Tool output (JSON string if structured)


# ------------------------------
# Requests and Results
# ------------------------------


class CompletionRequest(BaseModel):
    """Provider-agnostic completion request.

    This represents a request to generate a completion from any LLM provider.
    Providers translate this to their native request formats.
    """
    messages: list[Message]
    tools: list[Tool] | None = None
    tool_choice: Literal["auto", "required", "none"] | str | None = None  # str for specific tool
    max_tokens: int | None = None
    temperature: float | None = None

    # Provider-specific extensions can be passed via extra fields
    model_config = {"extra": "allow"}


class Usage(BaseModel):
    """Provider-agnostic usage information."""
    input_tokens: int
    output_tokens: int
    total_tokens: int


class CompletionResult(BaseModel):
    """Provider-agnostic completion result.

    Represents the response from any LLM provider. Providers translate their
    native response formats to this structure.
    """
    id: str  # Completion ID
    content: str | None  # Assistant text response (None if only tool calls)
    tool_calls: list[ToolCall] | None = None  # Tool calls from the model
    usage: Usage | None = None  # Token usage

    # Provider-specific metadata
    model_config = {"extra": "allow"}
