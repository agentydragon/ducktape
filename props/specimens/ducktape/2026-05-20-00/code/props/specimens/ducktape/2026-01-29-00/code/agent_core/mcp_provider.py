"""MCP-based tool provider wrapping fastmcp.Client.

This module has the MCP/fastmcp dependency. Import from here when using MCP tools.
"""

from __future__ import annotations

from typing import Any

from fastmcp.client import Client
from fastmcp.client.client import CallToolResult as FastMCPCallToolResult
from mcp import types as mcp_types

from agent_core.tool_provider import ImageContent, TextContent, ToolResult, ToolSchema


class MCPToolProvider:
    """Tool provider that wraps a FastMCP Client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def list_tools(self) -> list[ToolSchema]:
        """Return available tools from the MCP client."""
        mcp_tools = await self._client.list_tools()
        return [ToolSchema(name=t.name, description=t.description or "", input_schema=t.inputSchema) for t in mcp_tools]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool via the MCP client."""
        fastmcp_result = await self._client.call_tool(name, arguments, raise_on_error=False)
        return fastmcp_result_to_tool_result(fastmcp_result)


def tool_result_to_mcp(result: ToolResult) -> mcp_types.CallToolResult:
    """Convert our ToolResult to mcp.types.CallToolResult."""
    mcp_content: list[mcp_types.TextContent | mcp_types.ImageContent] = []
    for block in result.content:
        if isinstance(block, TextContent):
            mcp_content.append(mcp_types.TextContent(type="text", text=block.text))
        elif isinstance(block, ImageContent):
            mcp_content.append(mcp_types.ImageContent(type="image", mimeType=block.mime_type, data=block.data))
        else:
            raise TypeError(f"Unhandled content block type: {type(block)}")

    return mcp_types.CallToolResult(
        content=mcp_content, structuredContent=result.structured_content, isError=result.is_error
    )


def fastmcp_result_to_tool_result(fastmcp_result: FastMCPCallToolResult) -> ToolResult:
    """Convert fastmcp.client.CallToolResult to our ToolResult.

    FastMCP client returns its own CallToolResult dataclass with snake_case fields
    (is_error, structured_content), not mcp.types.CallToolResult.
    """
    content: list[TextContent | ImageContent] = []
    for block in fastmcp_result.content or []:
        if isinstance(block, mcp_types.TextContent):
            content.append(TextContent(text=block.text))
        elif isinstance(block, mcp_types.ImageContent):
            content.append(ImageContent(mime_type=block.mimeType, data=block.data))
        else:
            raise TypeError(f"Unhandled MCP content block type: {type(block)}")

    return ToolResult(
        content=content, structured_content=fastmcp_result.structured_content, is_error=bool(fastmcp_result.is_error)
    )
