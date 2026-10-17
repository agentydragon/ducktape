"""Extract tool schemas from FastMCP servers for CompactDisplayHandler.

Introspects tool return types from FastMCP server instances to build
schema registries for typed display rendering.

Uses FastMCP internal `_local_provider._components` because the public
`list_tools()` API is async and callers need sync access.
"""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING, Any, get_type_hints

from fastmcp.tools import Tool
from fastmcp.tools.function_tool import FunctionTool
from pydantic import BaseModel, ConfigDict, create_model

from mcp_infra.prefix import MCPMountPrefix

if TYPE_CHECKING:
    from fastmcp.server import FastMCP


def _iter_tools(server: FastMCP) -> list[Tool]:
    """Get tools from FastMCP server via sync internal access."""
    return [c for c in server._local_provider._components.values() if isinstance(c, Tool)]


def input_model_for_tool(tool: FunctionTool) -> type[BaseModel]:
    """Build a Pydantic model for a FunctionTool's exposed arguments.

    FastMCP's native tool API takes one typed function parameter per argument.
    Reconstructing a model from that signature keeps typed clients and rich
    display validation without a custom Tool subclass.
    """
    try:
        signature = inspect.signature(tool.fn)
    except ValueError, TypeError:
        return create_model(f"{tool.name}Input", __config__=ConfigDict(extra="forbid"))

    try:
        hints = get_type_hints(tool.fn, include_extras=True)
    except NameError, TypeError, AttributeError:
        hints = {}

    properties = tool.parameters.get("properties", {})
    fields: dict[str, Any] = {}
    for name in properties:
        parameter = signature.parameters.get(name)
        if parameter is None:
            continue
        annotation = hints.get(name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = Any
        default = parameter.default
        if default is inspect.Parameter.empty:
            default = ...
        fields[name] = (annotation, default)

    model_name = re.sub(r"\W+", "_", f"{tool.name.title()}Input")
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)


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
            except ValueError, TypeError:
                continue

            try:
                hints = get_type_hints(tool.fn, include_extras=True)
            except NameError, TypeError, AttributeError:
                hints = {}
            return_type = hints.get("return", sig.return_annotation)
            if inspect.isclass(return_type) and issubclass(return_type, BaseModel):
                schemas[(server_prefix, tool.name)] = return_type

    return schemas


def extract_tool_input_schemas(
    servers: dict[MCPMountPrefix, FastMCP],
) -> dict[tuple[MCPMountPrefix, str], type[BaseModel]]:
    """Extract tool input types from FastMCP servers.

    Includes FunctionTools using FastMCP's native function-signature API.
    """
    schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {}

    for server_prefix, server in servers.items():
        for tool in _iter_tools(server):
            if not isinstance(tool, FunctionTool):
                continue
            schemas[(server_prefix, tool.name)] = input_model_for_tool(tool)

    return schemas
