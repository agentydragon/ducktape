"""Agent workspace management.

Agent workspaces are persistent directories bind-mounted to /workspace in containers.
Used for files created by the agent during operation (fetched snapshots, outputs, etc.).

Note: Agent package contents (/init, /agent.md) are in the Docker image, not the workspace.
Workspaces survive container restarts and app quits.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from platformdirs import user_data_path


class WorkspaceManager:
    """Manages agent workspace directories.

    Use from_env() for production, inject base_path directly for tests.
    """

    def __init__(self, base_path: Path):
        self._base_path = base_path

    @classmethod
    def from_env(cls) -> WorkspaceManager:
        """Create from ADGN_WORKSPACES_DIR or platform default."""
        env_path = os.environ.get("ADGN_WORKSPACES_DIR")
        if env_path:
            return cls(Path(env_path))
        return cls(user_data_path("adgn") / "workspaces")

    def get_path(self, agent_run_id: UUID) -> Path:
        return self._base_path / str(agent_run_id)
