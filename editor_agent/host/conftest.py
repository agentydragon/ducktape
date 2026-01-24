from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from pathlib import Path

import docker
import pytest
from rules_python.python.runfiles import runfiles

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from mcp_infra.testing.fixtures import *  # noqa: F403

EDITOR_IMAGE_TAG = "adgn-editor:latest"


def _get_runfiles_path(relative_path: str) -> Path:
    """Get path to a file in Bazel runfiles."""
    r = runfiles.Create()
    path = r.Rlocation(f"_main/{relative_path}")
    if path:
        return Path(path)

    # Fallback: check bazel-bin for local dev
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "bazel-bin" / relative_path


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip Docker tests when Docker daemon is not available."""
    if item.get_closest_marker("requires_docker") is None:
        return

    client = None
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


@pytest.fixture(scope="session")
def editor_image_id():
    """Load editor agent image from Bazel :load target."""
    load_script = _get_runfiles_path("editor_agent/runtime/load.sh")

    result = subprocess.run(
        [load_script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER_CLI_EXPERIMENTAL": "enabled"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load editor image: {result.stderr}")

    return EDITOR_IMAGE_TAG
