"""Extract tool schemas from FastMCP servers for RichDisplayHandler.

Introspects tool return types from FastMCP server instances to build
schema registries for typed display rendering.

Uses FastMCP internal `_local_provider._components` because the public
`list_tools()` API is async and callers need sync access.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool import Tool
from pydantic import BaseModel

from mcp_infra.flat_tool import FlatTool
from mcp_infra.prefix import MCPMountPrefix

if TYPE_CHECKING:
    from fastmcp.server import FastMCP


def _iter_tools(server: FastMCP) -> list[Tool]:
    """Get tools from FastMCP server via sync internal access."""
    return [c for c in server._local_provider._components.values() if isinstance(c, Tool)]


def extract_tool_schemas(servers: dict[MCPMountPrefix, FastMCP]) -> dict[tuple[MCPMountPrefix, str], type[BaseModel]]:
    """Extract tool result types from FastMCP servers.

    Only includes FunctionTools with Pydantic BaseModel return annotations.
    """
    schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {}

    for server_prefix, server in servers.items():
        for tool in _iter_tools(server):
            if not isinstance(tool, FunctionTool):
                continue

            try:
                sig = inspect.signature(tool.fn)
            except (ValueError, TypeError):
                continue

            return_type = sig.return_annotation
            if inspect.isclass(return_type) and issubclass(return_type, BaseModel):
                schemas[(server_prefix, tool.name)] = return_type

    return schemas


def extract_tool_input_schemas(
    servers: dict[MCPMountPrefix, FastMCP],
) -> dict[tuple[MCPMountPrefix, str], type[BaseModel]]:
    """Extract tool input types from FastMCP servers.

    Only includes FlatTools or FunctionTools with a single Pydantic BaseModel parameter annotation.
    """
    schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {}

    for server_prefix, server in servers.items():
        for tool in _iter_tools(server):
            # Check for FlatTool first
            if isinstance(tool, FlatTool):
                schemas[(server_prefix, tool.name)] = tool.input_model
                continue

            # Regular FunctionTools: check for single Pydantic param
            if not isinstance(tool, FunctionTool):
                continue

            try:
                sig = inspect.signature(tool.fn)
            except (ValueError, TypeError):
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
                schemas[(server_prefix, tool.name)] = pydantic_params[0].annotation

    return schemas
