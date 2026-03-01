"""MCP resource content extraction utilities."""

from __future__ import annotations

from mcp import types as mcp_types
from more_itertools import one
from pydantic import TypeAdapter


def parse_tool_result_as[T](result: mcp_types.CallToolResult, model: type[T]) -> T:
    """Extract the single TextContent from a tool result and parse it as a Pydantic model."""
    item = one(result.content)
    assert isinstance(item, mcp_types.TextContent)
    return TypeAdapter(model).validate_json(item.text)


def extract_single_text_content(res: list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents]) -> str:
    """Return the single text part from a read_resource result or raise.

    - Requires exactly one TextResourceContents part.
    - Raises RuntimeError if zero or multiple text parts, or if blob content present.
    """
    text_parts = [p for p in res if isinstance(p, mcp_types.TextResourceContents)]
    if any(isinstance(p, mcp_types.BlobResourceContents) for p in res):
        raise RuntimeError("expected a single text part, found blob content")
    text: str | None = one(text_parts).text
    if text is None:
        raise RuntimeError("text content part missing text payload")
    return text
