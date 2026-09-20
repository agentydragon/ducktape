"""Answers `item/tool/call` server requests for tools declared via `thread/start.dynamicTools`, for
driving Codex's client-supplied-tool role from a test or capture, not a real subprocess."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agentplane.native.codex import wire


@dataclass(frozen=True)
class DynamicTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Returns `contentItems` (e.g. `[{"type": "inputText", "text": "..."}]`); Codex has no error
    # marker on the call itself, so a failing handler answers with `success=False` content instead.
    handler: Callable[[dict[str, Any]], list[dict[str, Any]]]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class DynamicToolServer:
    """Answers one `item/tool/call` server request per registered tool name."""

    def __init__(self, tools: Mapping[str, DynamicTool]):
        self._tools = tools

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    async def respond(self, frame: dict[str, Any]) -> wire.Response | None:
        match wire.parse_frame(frame):
            case wire.ServerRequest(id=request_id, method="item/tool/call", params=params) if params is not None:
                tool = self._tools.get(params.get("tool", ""))
                if tool is None:
                    return wire.Response(
                        id=request_id,
                        result={
                            "contentItems": [{"type": "inputText", "text": f"unknown tool {params.get('tool')!r}"}],
                            "success": False,
                        },
                    )
                content = tool.handler(params.get("arguments") or {})
                return wire.Response(id=request_id, result={"contentItems": content, "success": True})
        return None
