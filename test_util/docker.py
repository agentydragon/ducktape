"""Docker image loading utilities for tests.

Provides a unified interface for loading OCI images from Bazel oci_load targets
into the local Docker daemon during test execution.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from pathlib import Path

import docker
import pytest
from rules_python.python.runfiles import runfiles


def get_runfiles_path(relative_path: str) -> Path:
    """Get path to a file in Bazel runfiles.

    Args:
        relative_path: Path relative to repository root (e.g., "mcp_infra/testing/python_slim_load.sh")

    Returns:
        Absolute path to the file, either from runfiles or bazel-bin fallback.
    """
    r = runfiles.Create()
    path = r.Rlocation(f"_main/{relative_path}")
    if path:
        return Path(path)

    # Fallback: check bazel-bin for local dev (walk up to find repo root)
    current = Path(__file__).parent
    while current != current.parent:
        if (current / "MODULE.bazel").exists():
            return current / "bazel-bin" / relative_path
        current = current.parent

    # Last resort: assume 1 level up from this file
    return Path(__file__).parent.parent / "bazel-bin" / relative_path


def load_bazel_image(load_script_path: str, image_tag: str) -> str:
    """Load an OCI image from a Bazel oci_load target.

    Args:
        load_script_path: Relative path to the load.sh script (e.g., "mcp_infra/testing/python_slim_load.sh")
        image_tag: The expected image tag after loading (e.g., "python-slim:test")

    Returns:
        The image tag that was loaded.

    Raises:
        RuntimeError: If loading the image fails.
    """
    load_script = get_runfiles_path(load_script_path)

    result = subprocess.run(
        [load_script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER_CLI_EXPERIMENTAL": "enabled"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load image {image_tag}: {result.stderr}")

    return image_tag


def skip_if_docker_unavailable(item: pytest.Item) -> None:
    """Skip test if Docker daemon is not available.

    Used by pytest_runtest_setup for tests marked with @pytest.mark.requires_docker.
    """
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


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Pytest hook to skip Docker tests when Docker daemon is not available.

    Import this function in conftest.py to enable automatic Docker test skipping:
        from test_util.docker import pytest_runtest_setup  # noqa: F401
    """
    skip_if_docker_unavailable(item)


# Image tags and load scripts for shared test images
PYTHON_SLIM_IMAGE_TAG = "python-slim:test"
PYTHON_SLIM_LOAD_SCRIPT = "mcp_infra/testing/python_slim_load.sh"


@pytest.fixture(scope="session")
def python_slim_image():
    """Load python-slim image from Bazel :python_slim_load target."""
    return load_bazel_image(PYTHON_SLIM_LOAD_SCRIPT, PYTHON_SLIM_IMAGE_TAG)
