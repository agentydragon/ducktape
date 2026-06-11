"""Pytest configuration for mcp_infra/exec tests."""

import pytest

# Import fixtures from source modules
from mcp_infra.testing.docker_fixtures import docker_exec_server
from mcp_infra.testing.fixtures import *  # noqa: F403
from openai_utils.testing.fixtures import live_openai_model, mock_or_live
from third_party.debian_slim.fixtures import debian_slim_image


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
