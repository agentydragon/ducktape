"""Mounted server wrapper bundling prefix and server instance."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from pydantic.networks import AnyUrl

from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import add_resource_prefix

if TYPE_CHECKING:
    from fastmcp.tools import FunctionTool


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
        # Access tool: runtime.server.exec_tool.name

        # Build tool call requests via TypedBootstrapBuilder:
        builder = TypedBootstrapBuilder()
        call = builder.call(runtime.prefix, runtime.server.exec_tool.name, ExecInput(...))
    """

    prefix: MCPMountPrefix
    server: T

    def tool_name(self, tool: "FunctionTool") -> str:
        """Get fully-qualified MCP tool name.

        Args:
            tool: FunctionTool from self.server.some_tool

        Returns:
            Fully-qualified tool name (e.g., "lint_submit_submit_result")

        Example:
            submit_tool_name = comp.lint_submit.tool_name(comp.lint_submit.server.submit_result_tool)
        """
        return build_mcp_function(self.prefix, tool.name)

    def add_resource_prefix(self, uri: str | AnyUrl) -> str:
        """Add this mount's prefix to a resource URI.

        Wrapper around FastMCP's add_resource_prefix using this mount's prefix.

        Args:
            uri: Resource URI (str or Pydantic AnyUrl)

        Returns:
            Prefixed URI string

        Example:
            prefixed = comp.compositor_meta.add_resource_prefix(
                meta_server.servers_list_resource.uri
            )
        """
        return add_resource_prefix(uri, self.prefix)
