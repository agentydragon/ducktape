"""Tests for agent_workspace module."""

from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel

from props.core.agent_workspace import WorkspaceManager


class TestWorkspaceManager:
    """Tests for WorkspaceManager."""

    def test_paths_are_isolated_per_run_id(self, tmp_path: Path) -> None:
        """Different run IDs produce non-overlapping paths."""
        mgr = WorkspaceManager(tmp_path)
        run1 = UUID("00000000-0000-0000-0000-000000000001")
        run2 = UUID("00000000-0000-0000-0000-000000000002")

        path1 = mgr.get_path(run1)
        path2 = mgr.get_path(run2)

        # Paths don't overlap (neither is prefix of the other)
        assert not str(path1).startswith(str(path2))
        assert not str(path2).startswith(str(path1))

    def test_from_env_uses_env_var(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ADGN_WORKSPACES_DIR env var is respected."""
        custom = tmp_path / "custom"
        monkeypatch.setenv("ADGN_WORKSPACES_DIR", str(custom))

        mgr = WorkspaceManager.from_env()
        run_id = UUID("00000000-0000-0000-0000-000000000001")

        assert mgr.get_path(run_id).parent == custom


if __name__ == "__main__":
    pytest_bazel.main()
