from __future__ import annotations

from mcp_infra.prefix import MCPMountPrefix

"""Canonical MCP tool naming helpers.

Single source of truth for building and parsing namespaced MCP tool names.

Format: ``{server}_{tool}``. A single underscore separates the server identifier
and tool name; the tool portion may itself contain underscores.
"""


def build_mcp_function(server: MCPMountPrefix, tool: str) -> str:
    """Return the fully-qualified tool name for the aggregated compositor surface.

    Args:
        server: Mount prefix (already validated via MCPMountPrefix type)
        tool: Tool name (must be non-empty)

    Returns:
        Fully-qualified tool name in format {server}_{tool}

    Raises:
        ValueError: If tool name is invalid
    """
    if not tool:
        raise ValueError("Tool name must be non-empty")

    return f"{server}_{tool}"


def parse_tool_name(name: str) -> tuple[MCPMountPrefix, str]:
    """Parse a tool name into (prefix, tool) tuple.

    Inverse of build_mcp_function(). Expects format: {prefix}_{tool}.
    Tool portion may contain underscores.

    Returns:
        Tuple of (mount_prefix, tool_name)

    Raises:
        ValueError: If name doesn't contain exactly one underscore separator,
                   or if either prefix or tool portion is empty or invalid.
    """

    def _err(detail: str) -> str:
        return f"Invalid tool name format: {name!r}. {detail}"

    prefix_str, _, tool = name.partition("_")
    if not prefix_str or not tool:
        raise ValueError(_err("Expected 'prefix_tool'."))

    # Validate and construct MCPMountPrefix
    try:
        prefix = MCPMountPrefix(prefix_str)
    except Exception as e:
        raise ValueError(_err(f"Invalid prefix: {e}")) from e

    return (prefix, tool)


# Internal helpers; avoid barrels
