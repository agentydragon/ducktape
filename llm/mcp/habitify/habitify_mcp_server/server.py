"""
Habitify MCP Server implementation.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import tools
from .config import load_api_key
from .types import Status


def create_habitify_mcp_server(
    debug: bool = False, log_level: str = "INFO", api_key: str | None = None, port: int = 3000
) -> FastMCP:
    """Create and configure a Habitify MCP server."""
    # Define MCP metadata
    server = FastMCP(
        "Habitify",
        description="Habitify API for habit tracking through Model Context Protocol",
        dependencies=["httpx", "python-dotenv"],
        debug=debug,
        log_level=log_level,
        port=port,  # Set the port in the server settings
    )

    # Validate API key is available (tools will retrieve it from environment)
    load_api_key(api_key_override=api_key, exit_on_missing=False)

    @server.tool()
    async def get_habits(include_archived: bool = False) -> dict[str, Any]:
        return await tools.get_habits(include_archived=include_archived)  # type: ignore[call-arg] - client injected by decorator

    @server.tool()
    async def get_habit(id: str | None = None, name: str | None = None) -> dict[str, Any]:
        return await tools.get_habit(id=id, name=name)  # type: ignore[call-arg] - client injected by decorator

    @server.tool()
    async def get_habit_status(
        id: str | None = None,
        name: str | None = None,
        date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """Get habit status for single date or date range.

        Single date: use 'date' (YYYY-MM-DD, defaults to today)

        Date range (inclusive): use one of:
        - start_date + end_date: specific range
        - start_date + days: N days from start
        - end_date + days: N days before end
        - days: N days ending today
        """
        return await tools.get_habit_status(  # type: ignore[call-arg] - client injected by decorator
            id=id, name=name, date=date, start_date=start_date, end_date=end_date, days=days
        )

    @server.tool()
    async def set_habit_status(
        id: str | None = None,
        name: str | None = None,
        status: Status = Status.COMPLETED,
        date: str | None = None,
        note: str | None = None,
        value: float | None = None,
    ) -> dict[str, Any]:
        return await tools.set_habit_status(  # type: ignore[call-arg] - client injected by decorator
            id=id, name=name, status=status.value, date=date, note=note, value=value
        )

    return server
