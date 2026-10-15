"""The console's non-secret deploy catalog (`haku.console.mcp_config.ConsoleConfigFile`).

Secret leaves stay out of it: the Deployment overlays them from Secrets through
`HAKU_CONSOLE__*` environment variables (`console.py` names each one).
"""

from __future__ import annotations

from typing import Any


def config() -> dict[str, Any]:
    return {
        # No Console-owned MCP execution server is currently active. Agent calls use manual review
        # if a server is configured again; approval policy alone never creates a backend.
        "auto_approval_policies": [{"id": "manual_review", "type": "never"}],
        "access_profiles": [
            {"id": "haku", "auto_approval_policy": "manual_review"},
            {"id": "manual-review", "auto_approval_policy": "manual_review"},
        ],
        "default_access_profile_id": "manual-review",
        # Static machine Agents retain their durable identity and operator ownership.
        "static_agents": {
            "haku": {
                "agent_id": "8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2",
                "display_name": "Haku",
                "access_profile_id": "haku",
            }
        },
        "mcp": {"servers": {}},
    }
