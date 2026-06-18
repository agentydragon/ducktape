"""Generic tool-visibility filter for FastMCP servers.

A `ToolFilter` is a server-agnostic allow/deny policy over tool names
(case-sensitive `fnmatch` globs). `ToolFilterMiddleware` enforces it on both
`tools/list` (hides disallowed tools) and `tools/call` (rejects them) — the
list hook alone is not a security boundary, since a client can call a tool it
was never shown. Nothing here is specific to any upstream; a read-only facade
is just one configuration (an allowlist of the upstream's read tools).
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult
from pydantic import BaseModel, Field


class ToolFilter(BaseModel):
    """Allow/deny policy over tool names.

    A tool is admitted iff it matches `allow` (when set) and matches no `deny`
    glob. Leaving `allow` unset opens the gate to everything not denied; set it
    for default-deny — the safe choice for a read-only boundary. Patterns are
    case-sensitive `fnmatch` globs (`read_*`, `search_nodes`).
    """

    allow: list[str] | None = Field(
        default=None, description="Glob allowlist. When set, a tool must match an entry to be exposed (default-deny)."
    )
    deny: list[str] | None = Field(
        default=None, description="Glob denylist, applied after `allow`; a match is always hidden and rejected."
    )

    def admits(self, tool_name: str) -> bool:
        if self.allow is not None and not any(fnmatchcase(tool_name, pattern) for pattern in self.allow):
            return False
        return self.deny is None or not any(fnmatchcase(tool_name, pattern) for pattern in self.deny)


class ToolFilterMiddleware(Middleware):
    """Enforces a `ToolFilter` on `tools/list` and `tools/call`."""

    def __init__(self, policy: ToolFilter) -> None:
        self._policy = policy

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: CallNext[Any, list[Tool]]) -> list[Tool]:
        tools = await call_next(context)
        return [tool for tool in tools if self._policy.admits(tool.name)]

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult]) -> ToolResult:
        if not self._policy.admits(context.message.name):
            raise ToolError(f"Tool {context.message.name!r} is not exposed by this server.")
        return await call_next(context)
