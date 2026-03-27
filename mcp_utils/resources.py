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
    """Return the text from the single TextResourceContents part, or raise."""
    item = one(res)
    if not isinstance(item, mcp_types.TextResourceContents):
        raise RuntimeError(f"expected TextResourceContents, got {type(item).__name__}")
    return item.text
