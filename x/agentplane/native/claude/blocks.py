"""Anthropic message content blocks: the same shapes ride inside Claude Code's stdio frames and in
the Messages requests it sends upstream."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Tag

from x.agentplane.native.tagged import UNKNOWN, tag_or_unknown


class TextBlock(BaseModel):
    type: Literal["text"]
    text: str


class ThinkingBlock(BaseModel):
    type: Literal["thinking"]
    thinking: str
    signature: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class UnknownBlock(BaseModel):
    """A block kind these models do not describe, such as an image or a redacted thinking block."""

    model_config = ConfigDict(extra="allow")

    type: str


ResultBlock = Annotated[
    Annotated[TextBlock, Tag("text")] | Annotated[UnknownBlock, Tag(UNKNOWN)],
    Discriminator(tag_or_unknown("type", frozenset({"text"}))),
]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: str | list[ResultBlock]
    is_error: bool = False

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))


Block = Annotated[
    Annotated[TextBlock, Tag("text")]
    | Annotated[ThinkingBlock, Tag("thinking")]
    | Annotated[ToolUseBlock, Tag("tool_use")]
    | Annotated[ToolResultBlock, Tag("tool_result")]
    | Annotated[UnknownBlock, Tag(UNKNOWN)],
    Discriminator(tag_or_unknown("type", frozenset({"text", "thinking", "tool_use", "tool_result"}))),
]


def blocks_of(content: str | list[Block]) -> list[Block]:
    """A message's blocks, with plain-string content as one text block."""
    return [TextBlock(type="text", text=content)] if isinstance(content, str) else content
