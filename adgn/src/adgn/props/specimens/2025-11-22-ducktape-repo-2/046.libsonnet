local I = import '../../specimens/lib.libsonnet';

// iss-046: servers/ directory contains no servers, __init__.py is misleading

I.issueOneOccurrence(
  rationale=|||
    The `adgn/src/adgn/agent/mcp_bridge/servers/__init__.py` file contains the docstring
    "MCP servers for the MCP bridge." but the servers/ directory contains no actual MCP servers.

    At this commit, the servers/ directory only contains:
    - __init__.py (with misleading docstring)
    - types.py (just type definitions: RunPhase and ApprovalStatus enums)

    The actual MCP server implementation is at `adgn/src/adgn/agent/mcp_bridge/server.py`
    (one level up, not in servers/).

    Additionally, there are two separate types.py files:
    - adgn/src/adgn/agent/mcp_bridge/types.py (parent directory)
    - adgn/src/adgn/agent/mcp_bridge/servers/types.py (servers subdirectory)

    This creates confusion about where types belong and what the servers/ directory is for.

    Fix:
    1. Move servers/types.py to mcp_bridge/types.py or merge with existing mcp_bridge/types.py
    2. Delete the now-empty servers/ directory
    3. Update any imports that reference servers/types.py

    This would eliminate the misleading directory structure and consolidate type definitions
    in one clear location.
  |||,
  properties=['truthfulness', 'no-dead-code'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/__init__.py': [1],  // Misleading docstring
    'adgn/src/adgn/agent/mcp_bridge/servers/types.py': [],  // Should be moved
  },
  gap_note=|||
    This pattern deserves a property like "no-misleading-module-structure": when a
    directory or module name implies certain contents (e.g., "servers" implying multiple
    server implementations), but actually contains something different (just type definitions),
    the structure should be refactored to match the name or the name should be changed to
    reflect the actual contents. This is distinct from general "truthfulness" as it specifically
    addresses package/module organization and the expectations set by directory/module naming.
  |||,
)
