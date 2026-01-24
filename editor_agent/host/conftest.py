from __future__ import annotations

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from mcp_infra.testing.fixtures import *  # noqa: F403
from test_util.docker import load_bazel_image, skip_if_docker_unavailable

EDITOR_IMAGE_TAG = "adgn-editor:latest"
EDITOR_LOAD_SCRIPT = "editor_agent/runtime/load.sh"


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip Docker tests when Docker daemon is not available."""
    skip_if_docker_unavailable(item)


@pytest.fixture(scope="session")
def editor_image_id():
    """Load editor agent image from Bazel :load target."""
    return load_bazel_image(EDITOR_LOAD_SCRIPT, EDITOR_IMAGE_TAG)
