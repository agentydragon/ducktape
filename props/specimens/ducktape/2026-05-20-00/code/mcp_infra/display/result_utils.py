"""Utilities for extracting display data from tool results."""

from __future__ import annotations

from agent_core.tool_provider import TextContent, ToolOutputData, ToolResult


def extract_display_data(result: ToolResult) -> ToolOutputData | str | None:
    """Extract display-friendly data from ToolResult.

    Prefers structured_content if present, otherwise extracts text from content blocks.
    """
    if result.structured_content is not None:
        return result.structured_content
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if texts:
        return "\n".join(texts) if len(texts) > 1 else texts[0]
    return None
