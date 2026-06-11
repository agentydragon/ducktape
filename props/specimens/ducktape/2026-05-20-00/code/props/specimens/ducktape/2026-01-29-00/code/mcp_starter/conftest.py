from collections.abc import AsyncIterator

import pytest_asyncio
from fastmcp import Client
from fastmcp.mcp_config import MCPConfig, StdioMCPServer


@pytest_asyncio.fixture
async def mcp_client() -> AsyncIterator[Client]:
    """Async FastMCP client connected to the starter server via stdio.

    Yields a connected Client so tests can call methods without context managers.
    """
    async with Client(
        MCPConfig(mcpServers={"starter": StdioMCPServer(command="python", args=["-m", "adgn_mcp_starter", "--debug"])})
    ) as client:
        yield client
