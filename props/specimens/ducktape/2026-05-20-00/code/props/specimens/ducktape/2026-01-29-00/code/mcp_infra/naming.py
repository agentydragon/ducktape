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

    parts = name.split("_", 1)
    if len(parts) != 2:
        raise ValueError(_err("Expected 'prefix_tool'."))
    prefix_str, tool = parts[0], parts[1]
    if not prefix_str:
        raise ValueError(_err("Prefix portion is empty."))
    if not tool:
        raise ValueError(_err("Tool portion is empty."))

    # Validate and construct MCPMountPrefix
    try:
        prefix = MCPMountPrefix(prefix_str)
    except Exception as e:
        raise ValueError(_err(f"Invalid prefix: {e}")) from e

    return (prefix, tool)


def tool_prefix(server: MCPMountPrefix) -> str:
    """Return the namespaced prefix for all tools exposed by mount prefix.

    Args:
        server: Mount prefix (already validated via MCPMountPrefix type)

    Returns:
        Namespaced prefix with trailing underscore (e.g., "runtime_")
    """
    return f"{server}_"


def tool_matches(name: str, *, server: MCPMountPrefix, tool: str) -> bool:
    """Return True when ``name`` refers to the specified server/tool.

    Args:
        name: Tool name to check
        server: Mount prefix (already validated)
        tool: Tool name to match

    Returns:
        True if name matches the server/tool combination
    """
    return name == build_mcp_function(server, tool)


def server_matches(name: str, *, server: MCPMountPrefix) -> bool:
    """Return True when ``name`` belongs to the specified server.

    Args:
        name: Tool name to check
        server: Mount prefix (already validated)

    Returns:
        True if name starts with the server's tool prefix
    """
    return name.startswith(tool_prefix(server))


# Internal helpers; avoid barrels
