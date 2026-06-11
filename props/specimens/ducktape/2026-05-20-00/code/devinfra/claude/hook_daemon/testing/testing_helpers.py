"""Shared test helpers for hook daemon tests."""

from pathlib import Path

import yaml

from devinfra.claude.hook_daemon.config import GitShimConfig, ProfileConfig
from devinfra.claude.session_paths import SessionPaths

PROFILE_FILENAME = "profile.yaml"

TEST_PROFILE = ProfileConfig(
    idle_watchdog=True, git_shim=GitShimConfig(block_amend=True, block_stash=True, block_add_all=True)
)


def setup_daemon_project(base_dir: Path, paths: SessionPaths) -> tuple[Path, Path]:
    """Create minimal project dir with profile config and return (project_dir, env_file).

    Used by both the daemon_paths fixture and test_parallel_cold_start (which needs
    os.environ for child process inheritance instead of monkeypatch).
    """
    project_dir = base_dir / "project"
    project_dir.mkdir(exist_ok=True)
    (project_dir / PROFILE_FILENAME).write_text(yaml.dump(TEST_PROFILE.model_dump(mode="json")))
    env_file = paths.session_dir / "sessionstart-hook-0.sh"
    return project_dir, env_file
