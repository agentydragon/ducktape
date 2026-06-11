"""MCP domain types with Pydantic validation.

Note: This module imports fastmcp which is slow to load (~2.5s). For code that
only needs MCPMountPrefix (e.g., policy evaluation), import from mcp_infra.prefix
instead.
"""

from __future__ import annotations

from fastmcp.mcp_config import MCPServerTypes
from fastmcp.server import FastMCP
from pydantic import BaseModel

# MCP server specs: either typed specs (MCPServerTypes) or in-process server instances (FastMCP)
McpServerSpecs = dict[str, MCPServerTypes | FastMCP]


class SimpleOk(BaseModel):
    """Minimal ack type for tools that just signal success."""

    ok: bool = True
