"""MCP resource content extraction utilities."""

from __future__ import annotations

from collections.abc import Iterable

from mcp import types as mcp_types


def extract_text_from_tool_content(
    content: Iterable[
        mcp_types.TextContent
        | mcp_types.ImageContent
        | mcp_types.AudioContent
        | mcp_types.ResourceLink
        | mcp_types.EmbeddedResource
    ],
) -> str | None:
    """Extract text from MCP CallToolResult content blocks.

    Returns the first TextContent.text found, or None if no text content exists.
    """
    for item in content:
        if isinstance(item, mcp_types.TextContent):
            return item.text
    return None


def extract_single_text_content(res: list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents]) -> str:
    """Return the single text part from a read_resource result or raise.

    - Requires exactly one TextResourceContents part.
    - Raises RuntimeError if zero or multiple text parts, or if blob content present.
    """
    text_parts = [p for p in res if isinstance(p, mcp_types.TextResourceContents)]
    if any(isinstance(p, mcp_types.BlobResourceContents) for p in res):
        raise RuntimeError("expected a single text part, found blob content")
    if len(text_parts) != 1:
        raise RuntimeError(f"expected exactly one text part, found {len(text_parts)}")
    text: str | None = text_parts[0].text
    if text is None:
        raise RuntimeError("text content part missing text payload")
    return text
