"""Pytest configuration for mcp_infra/exec tests."""

import pytest

# Import fixtures from source modules
from mcp_infra.testing.docker_fixtures import docker_exec_server_py312slim
from mcp_infra.testing.fixtures import *  # noqa: F403
from test_util.docker import pytest_runtest_setup, python_slim_image


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
