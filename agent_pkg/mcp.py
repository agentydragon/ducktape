"""MCP client utilities for agent init scripts.

Provides helpers for connecting to the MCP server from within containers.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.client import Client
from mcp.types import InitializeResult

from mcp_utils.resources import extract_single_text_content


def print_mcp_env() -> None:
    """Print MCP environment variables for debugging."""
    print(f"MCP_SERVER_URL: {os.environ.get('MCP_SERVER_URL', '(not set)')}")


def get_mcp_url() -> str:
    """Get MCP server URL from environment.

    Raises:
        KeyError: If MCP_SERVER_URL is not set.
    """
    return os.environ["MCP_SERVER_URL"]


def get_mcp_token() -> str:
    """Get MCP auth token from environment.

    Raises:
        KeyError: If MCP_SERVER_TOKEN is not set.
    """
    return os.environ["MCP_SERVER_TOKEN"]


@asynccontextmanager
async def mcp_client_from_env() -> AsyncIterator[tuple[Client, InitializeResult]]:
    """Create an MCP client from environment variables.

    Uses MCP_SERVER_URL and MCP_SERVER_TOKEN from the environment.

    Yields:
        Tuple of (Client, InitializeResult):
        - client: Connected FastMCP Client instance
        - init_result: Server info including capabilities, instructions, protocol version

    Raises:
        KeyError: If required environment variables are not set.
        RuntimeError: If client does not initialize properly.
    """
    url = get_mcp_url()
    token = get_mcp_token()

    async with Client(url, auth=token) as client:
        init_result = client.initialize_result
        if init_result is None:
            raise RuntimeError("Client did not initialize properly")
        yield client, init_result


async def read_text_resource(client: Client[Any], uri: str) -> str:
    """Read a text resource and return its content."""
    contents = await client.read_resource(uri)
    return extract_single_text_content(contents)
