"""Canonical MCP tool naming helpers.

Single source of truth for building namespaced MCP tool names.

Format: ``{server}_{tool}``. A single underscore separates the server identifier
and tool name; the tool portion may itself contain underscores.
"""

from __future__ import annotations


def build_mcp_function(server: str, tool: str) -> str:
    """Return the fully-qualified tool name for an aggregated MCP server surface.

    Args:
        server: Mount prefix
        tool: Tool name (must be non-empty)

    Returns:
        Fully-qualified tool name in format {server}_{tool}

    Raises:
        ValueError: If tool name is invalid
    """
    if not tool:
        raise ValueError("Tool name must be non-empty")

    return f"{server}_{tool}"
