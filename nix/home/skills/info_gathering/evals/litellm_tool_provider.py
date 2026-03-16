"""LiteLLM <-> ToolProvider wiring.

Converts ToolProvider tool schemas to LiteLLM ToolParam format and extracts
tool result content for LiteLLM conversation history messages.
"""

import json
from typing import Any

from agent_core.tool_provider import TextContent, ToolProvider, ToolResult

ToolParam = dict[str, Any]


async def tool_params_from_provider(provider: ToolProvider) -> list[ToolParam]:
    """Convert ToolProvider schemas to LiteLLM ToolParam list."""
    return [
        {"type": "function", "function": {"name": s.name, "description": s.description, "parameters": s.input_schema}}
        for s in await provider.list_tools()
    ]


def tool_result_content(result: ToolResult) -> str:
    """Extract string content from a ToolResult for use in LiteLLM tool messages."""
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    return " ".join(texts) if texts else ("error" if result.is_error else "OK")
