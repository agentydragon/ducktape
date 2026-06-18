"""Generic tool-visibility filter for FastMCP servers.

A `ToolFilter` is a server-agnostic allow/deny policy over exact tool names.
`ToolFilterMiddleware` enforces it on both `tools/list` (hides disallowed tools)
and `tools/call` (rejects them) — the list hook alone is not a security
boundary, since a client can call a tool it was never shown. Nothing here is
specific to any upstream; a read-only facade is just an allowlist of the
upstream's read tools.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult
from pydantic import BaseModel, Field


class ToolFilter(BaseModel):
    """Allow/deny policy over exact tool names.

    A tool is admitted iff it is in `allow` (when set) and not in `deny`.
    Leaving `allow` unset admits everything not denied; set it for default-deny —
    the safe choice for a read-only boundary.
    """

    allow: set[str] | None = Field(
        default=None, description="Exact tool names to expose. When set, a tool must be listed (default-deny)."
    )
    deny: set[str] | None = Field(
        default=None, description="Exact tool names to hide and reject, applied after `allow`."
    )

    def admits(self, tool_name: str) -> bool:
        if self.allow is not None and tool_name not in self.allow:
            return False
        return self.deny is None or tool_name not in self.deny


class ToolFilterMiddleware(Middleware):
    """Enforces a `ToolFilter` on `tools/list` and `tools/call`."""

    def __init__(self, policy: ToolFilter) -> None:
        self._policy = policy

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Sequence[Tool]]
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [tool for tool in tools if self._policy.admits(tool.name)]

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult]) -> ToolResult:
        if not self._policy.admits(context.message.name):
            raise ToolError(f"Tool {context.message.name!r} is not exposed by this server.")
        return await call_next(context)
