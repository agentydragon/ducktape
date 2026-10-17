"""A mounted server paired with its prefix and instance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from pydantic.networks import AnyUrl

from mcp_infra.naming import build_mcp_function
from mcp_infra.resource_utils import add_resource_prefix
from x.agent_cli.compositor.prefix import MCPMountPrefix


@dataclass
class Mounted[T: FastMCP]:
    """A mounted server with its mount prefix and instance."""

    prefix: MCPMountPrefix
    server: T

    def tool_name(self, tool: str | Callable[..., Any]) -> str:
        """Return the fully qualified tool name for a tool name or function."""
        tool_name = tool if isinstance(tool, str) else tool.__name__
        return build_mcp_function(self.prefix, tool_name)

    def add_resource_prefix(self, uri: str | AnyUrl) -> str:
        """Add this mount's prefix to a resource URI."""
        return add_resource_prefix(uri, self.prefix)
