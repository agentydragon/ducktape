"""Server context for Habitify MCP server."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from .config import load_api_key
from .habitify_client import HabitifyClient


def make_lifespan(api_key: str | None = None):
    """Create a lifespan context manager for the Habitify MCP server.

    If api_key is provided, use it directly. Otherwise load from environment.
    """

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[HabitifyClient]:
        """Initialize client once at server startup, share across all requests."""
        key = api_key or load_api_key(exit_on_missing=False)
        if not key:
            raise RuntimeError(
                "HABITIFY_API_KEY environment variable is required. Set it in .env or pass via --api-key."
            )
        async with HabitifyClient(api_key=key) as client:
            yield client

    return lifespan
