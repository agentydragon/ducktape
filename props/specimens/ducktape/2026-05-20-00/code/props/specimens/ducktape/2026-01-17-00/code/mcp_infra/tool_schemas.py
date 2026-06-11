"""Extract tool schemas from FastMCP servers for RichDisplayHandler.

Introspects tool return types from FastMCP server instances to build
schema registries for typed display rendering.

Note: Uses FastMCP internal `_tool_manager._tools` because the public
`get_tools()` API is async and callers need sync access. The `_tools`
dict is stable across FastMCP versions.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from fastmcp.tools import FunctionTool
from pydantic import BaseModel

from mcp_infra.prefix import MCPMountPrefix

if TYPE_CHECKING:
    from fastmcp.server import FastMCP


def extract_tool_schemas(servers: dict[MCPMountPrefix, FastMCP]) -> dict[tuple[MCPMountPrefix, str], type[BaseModel]]:
    """Extract tool result types from FastMCP servers.

    Args:
        servers: Mapping of MCPMountPrefix -> FastMCP instance

    Returns:
        Mapping of (server_prefix, tool_name) -> Pydantic result type

    Only includes FunctionTools with Pydantic BaseModel return annotations.
    """
    schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {}

    for server_prefix, server in servers.items():
        # FastMCP public API (get_tools) is async; use internal _tools dict for sync access
        tools = server._tool_manager._tools
        for tool_name, tool in tools.items():
            if not isinstance(tool, FunctionTool):
                continue

            try:
                sig = inspect.signature(tool.fn)
            except (ValueError, TypeError):
                # Built-in or C extension functions can't be introspected
                continue

            return_type = sig.return_annotation
            if inspect.isclass(return_type) and issubclass(return_type, BaseModel):
                schemas[(server_prefix, tool_name)] = return_type

    return schemas


def extract_tool_input_schemas(
    servers: dict[MCPMountPrefix, FastMCP],
) -> dict[tuple[MCPMountPrefix, str], type[BaseModel]]:
    """Extract tool input types from FastMCP servers.

    Args:
        servers: Mapping of MCPMountPrefix -> FastMCP instance

    Returns:
        Mapping of (server_prefix, tool_name) -> Pydantic input type

    Only includes FunctionTools with a single Pydantic BaseModel parameter annotation.
    Handles both regular tools and flat-model tools (with _mcp_flat_input_model).
    """
    schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {}

    for server_prefix, server in servers.items():
        # FastMCP public API (get_tools) is async; use internal _tools dict for sync access
        tools = server._tool_manager._tools
        for tool_name, tool in tools.items():
            if not isinstance(tool, FunctionTool):
                continue

            # Check for flat-model tools first (these have flattened signatures)
            flat_model = getattr(tool.fn, "_mcp_flat_input_model", None)
            if flat_model is not None and issubclass(flat_model, BaseModel):
                schemas[(server_prefix, tool_name)] = flat_model
                continue

            # Regular tools: check for single Pydantic param
            try:
                sig = inspect.signature(tool.fn)
            except (ValueError, TypeError):
                # Built-in or C extension functions can't be introspected
                continue

            params = list(sig.parameters.values())
            pydantic_params = [
                p
                for p in params
                if p.annotation != inspect.Parameter.empty
                and inspect.isclass(p.annotation)
                and issubclass(p.annotation, BaseModel)
            ]

            if len(pydantic_params) == 1:
                schemas[(server_prefix, tool_name)] = pydantic_params[0].annotation

    return schemas
