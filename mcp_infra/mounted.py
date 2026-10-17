"""Mounted server wrapper bundling prefix and server instance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from pydantic.networks import AnyUrl

from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import add_resource_prefix


@dataclass
class Mounted[T: FastMCP]:
    """A mounted server with its mount prefix and instance.

    This bundles the mount prefix and server together, eliminating the need
    for separate mount prefix constants.

    Example:
        runtime: Mounted[RuntimeServer] = Mounted(
            prefix=MCPMountPrefix("runtime"),
            server=RuntimeServer(...)
        )

        # Access prefix: runtime.prefix
        # Access server: runtime.server
        # Access tool: runtime.server.exec_tool

        # Get the fully-qualified tool name:
        tool_name = runtime.tool_name(runtime.server.exec_tool)
    """

    prefix: MCPMountPrefix
    server: T

    def tool_name(self, tool: str | Callable[..., Any]) -> str:
        """Get the fully-qualified MCP tool name from its name or registered function.

        Args:
            tool: Tool name or function returned by FastMCP's ``@server.tool`` decorator.

        Returns:
            Fully-qualified tool name (e.g., "lint_submit_submit_result")

        Example:
            submit_tool_name = comp.lint_submit.tool_name(comp.lint_submit.server.submit_result_tool)
        """
        tool_name = tool if isinstance(tool, str) else tool.__name__
        return build_mcp_function(self.prefix, tool_name)

    def add_resource_prefix(self, uri: str | AnyUrl) -> str:
        """Add this mount's prefix to a resource URI.

        Wrapper around FastMCP's add_resource_prefix using this mount's prefix.

        Args:
            uri: Resource URI (str or Pydantic AnyUrl)

        Returns:
            Prefixed URI string

        Example:
            prefixed = comp.compositor_meta.add_resource_prefix("compositor://servers")
        """
        return add_resource_prefix(uri, self.prefix)
