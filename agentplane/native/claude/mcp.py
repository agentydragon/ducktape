"""A minimal MCP server answered entirely through `mcp_message` control requests, for driving
Claude Code's SDK MCP host role (`sdkMcpServers`) from a test or capture, not a real subprocess.

The tool registry and its execution run on a real `fastmcp.FastMCP` server reached over an
in-memory `fastmcp` `Client` (the same in-process pattern as
`x/postscanmail_mcp_server/conftest.py` and `agentplane/action_service/mcp_executor.py`); only the
outer JSON-RPC-over-`mcp_message` envelope, which is Claude's own control-protocol framing and not
part of MCP itself, is handled directly.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from mcp.types import TextContent

from agentplane.native.claude import driver, wire

# A JSON-RPC notification carries no `id`; the CLI's own gotcha requires an ack anyway, with this
# literal id, or its handshake with an SDK-hosted server times out.
_NOTIFICATION_RESPONSE_ID = 0


class DriverMcpServer:
    """Answers one driver-hosted server's `initialize` / `notifications/initialized` / `tools/list`
    / `tools/call` JSON-RPC round trip, each arriving as a correlated `mcp_message`."""

    def __init__(self, name: str, server: FastMCP):
        self.name = name
        self._client = Client(FastMCPTransport(server))

    async def respond(self, frame: dict[str, Any]) -> wire.ControlResponse | None:
        match wire.parse_frame(frame):
            case wire.ControlRequestFrame(
                request_id=request_id, request=wire.McpMessage(server_name=name, message=message)
            ) if name == self.name:
                return driver.mcp_response(request_id, await self._handle(message))
        return None

    async def _handle(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id", _NOTIFICATION_RESPONSE_ID)
        match message.get("method"):
            case "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": self.name, "version": "0.1"},
                    },
                }
            case "notifications/initialized":
                return {"jsonrpc": "2.0", "id": _NOTIFICATION_RESPONSE_ID, "result": {}}
            case "tools/list":
                async with self._client:
                    tools = await self._client.list_tools()
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
                            for tool in tools
                        ]
                    },
                }
            case "tools/call":
                params = message.get("params") or {}
                async with self._client:
                    result = await self._client.call_tool(
                        params.get("name", ""), params.get("arguments") or {}, raise_on_error=False
                    )
                content = [
                    {"type": "text", "text": block.text} for block in result.content if isinstance(block, TextContent)
                ]
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": content, "isError": result.is_error}}
            case _:
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
