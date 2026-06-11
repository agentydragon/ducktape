"""Pytest fixtures for agent_server tests."""

from __future__ import annotations

import json
from typing import Any

from agent_server.policies.policy_types import PolicyRequest
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix


def make_policy_request(server: MCPMountPrefix, tool: str, arguments: dict[str, Any] | None = None) -> PolicyRequest:
    """Helper to create PolicyRequest instances for tests.

    Args:
        server: MCP mount prefix (validated)
        tool: Tool name
        arguments: Tool arguments dict (will be JSON-encoded). Defaults to empty dict.

    Returns:
        PolicyRequest with arguments JSON-encoded as string.
    """
    return PolicyRequest(
        name=build_mcp_function(server, tool), arguments_json=json.dumps(arguments) if arguments is not None else None
    )
