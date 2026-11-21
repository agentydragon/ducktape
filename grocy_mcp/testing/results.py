"""Decoding FastMCP tool results in grocy_mcp tests."""

from __future__ import annotations

from typing import Any

from fastmcp.client import Client


def unwrap_result(result: Any) -> Any:
    """Extract unwrapped structured content from a FastMCP CallToolResult.

    FastMCP wraps non-object return types (lists, unions) in {"result": ...}
    with the x-fastmcp-wrap-result flag. We use structured_content for test
    assertions (raw JSON-like access) and unwrap the synthetic wrapper.
    """
    sc = result.structured_content
    assert sc is not None, "No structured_content in CallToolResult"
    if isinstance(sc, dict) and len(sc) == 1 and "result" in sc:
        return sc["result"]
    return sc


async def call_tool(client: Client, name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool and return its unwrapped structured content (see `unwrap_result`)."""
    return unwrap_result(await client.call_tool(name, arguments))
