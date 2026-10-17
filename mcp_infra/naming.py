"""Canonical MCP tool naming helpers.

Single source of truth for building and parsing namespaced MCP tool names.

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


def parse_tool_name(name: str) -> tuple[str, str]:
    """Parse a tool name into (prefix, tool) tuple.

    Inverse of build_mcp_function(). Expects format: {prefix}_{tool}.
    Tool portion may contain underscores.

    Returns:
        Tuple of (mount_prefix, tool_name). The prefix is a string; callers that
        need validation should use their domain-specific prefix type.

    Raises:
        ValueError: If either prefix or tool portion is empty.
    """

    def _err(detail: str) -> str:
        return f"Invalid tool name format: {name!r}. {detail}"

    prefix_str, _, tool = name.partition("_")
    if not prefix_str or not tool:
        raise ValueError(_err("Expected 'prefix_tool'."))

    return (prefix_str, tool)


# Internal helpers; avoid barrels
