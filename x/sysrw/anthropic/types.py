"""Pydantic models for Anthropic Messages API types.

These are stronger-typed wrappers around Anthropic SDK's TypedDict types.
Anthropic SDK types are runtime dicts; these are Pydantic BaseModels with validation.

Corresponds to:
- anthropic.types.MessageParam (TypedDict)
- anthropic.types.ContentBlockParam (TypedDict union)
- anthropic.types.TextBlockParam (TypedDict)
- anthropic.types.ToolUseBlockParam (TypedDict)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    """Message role enum.

    Corresponds to the 'role' field in anthropic.types.MessageParam.
    """

    USER = "user"
    ASSISTANT = "assistant"
    # Note: "system" is not a message role in Anthropic API - it's a separate parameter


class TextBlock(BaseModel):
    """Text content block. Corresponds to anthropic.types.TextBlockParam."""

    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """Tool use content block. Corresponds to anthropic.types.ToolUseBlockParam."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    """Tool result content block. Corresponds to anthropic.types.ToolResultBlockParam."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[TextBlock]
    is_error: bool = False


# Discriminated union for content blocks (corresponds to ContentBlockParam)
ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class Message(BaseModel):
    """A message in the Anthropic Messages API format.

    Pydantic wrapper for anthropic.types.MessageParam TypedDict.
    Provides runtime validation and proper attribute access instead of dict.get().
    """

    role: MessageRole
    content: str | list[ContentBlock]
