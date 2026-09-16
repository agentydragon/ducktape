"""A minimal MCP server answered entirely through `mcp_message` control requests, for driving
Claude Code's SDK MCP host role (`sdkMcpServers`) from a test or capture, not a real subprocess."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from x.agentplane.native.claude import driver, wire

# A JSON-RPC notification carries no `id`; the CLI's own gotcha requires an ack anyway, with this
# literal id, or its handshake with an SDK-hosted server times out.
_NOTIFICATION_RESPONSE_ID = 0


@dataclass(frozen=True)
class DriverTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Returns MCP content blocks, e.g. [{"type": "text", "text": "..."}].
    handler: Callable[[dict[str, Any]], list[dict[str, Any]]]


class DriverMcpServer:
    """Answers one driver-hosted server's `initialize` / `notifications/initialized` / `tools/list`
    / `tools/call` JSON-RPC round trip, each arriving as a correlated `mcp_message`."""

    def __init__(self, name: str, tools: Mapping[str, DriverTool]):
        self.name = name
        self._tools = tools

    async def respond(self, frame: dict[str, Any]) -> wire.ControlResponse | None:
        match wire.parse_frame(frame):
            case wire.ControlRequestFrame(
                request_id=request_id, request=wire.McpMessage(server_name=name, message=message)
            ) if name == self.name:
                return driver.mcp_response(request_id, self._handle(message))
        return None

    def _handle(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id", _NOTIFICATION_RESPONSE_ID)
        method = message.get("method")
        match method:
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
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
                            for tool in self._tools.values()
                        ]
                    },
                }
            case "tools/call":
                params = message.get("params") or {}
                tool = self._tools.get(params.get("name", ""))
                if tool is None:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"unknown tool {params.get('name')!r}"},
                    }
                content = tool.handler(params.get("arguments") or {})
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": content}}
            case _:
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
