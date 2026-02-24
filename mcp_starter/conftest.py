import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.mcp_config import MCPConfig, StdioMCPServer

from util.bazel.subprocess import python_env


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


@pytest_asyncio.fixture
async def mcp_client() -> AsyncIterator[Client]:
    """Async FastMCP client connected to the starter server via stdio.

    Yields a connected Client so tests can call methods without context managers.
    """
    async with Client(
        MCPConfig(
            mcpServers={
                "starter": StdioMCPServer(
                    command=sys.executable, args=["-m", "mcp_starter.main", "--debug"], env=python_env(inherit=False)
                )
            }
        )
    ) as client:
        yield client
