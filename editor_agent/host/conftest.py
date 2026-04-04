from __future__ import annotations

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from mcp_infra.testing.fixtures import *  # noqa: F403
from util.oci import OciImage, load_oci_image

_EDITOR = OciImage("_main/editor_agent/runtime/image_info.rloc", "adgn-editor:latest")


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


@pytest.fixture(scope="session")
def editor_image_id():
    """Load editor agent image and return its tag."""
    return load_oci_image(_EDITOR)
