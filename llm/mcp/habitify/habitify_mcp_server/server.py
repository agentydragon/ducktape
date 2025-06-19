"""
Habitify MCP Server implementation.
"""

import os
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import tools
from .types import Status


def create_habitify_mcp_server(
    debug: bool = False,
    log_level: str = "INFO",
    api_key: Optional[str] = None,
    port: int = 3000,
) -> FastMCP:
    """
    Create and configure a Habitify MCP server.

    Args:
        debug: Enable debug mode
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        api_key: Habitify API key (overrides environment variable)
        port: Port to use for SSE transport (default: 3000)

    Returns:
        Configured FastMCP server
    """
    # Define MCP metadata
    server = FastMCP(
        "Habitify",
        description="Habitify API for habit tracking through Model Context Protocol",
        dependencies=["httpx", "python-dotenv"],
        debug=debug,
        log_level=log_level,
        port=port,  # Set the port in the server settings
    )

    # Store API key in server context for tools - properly handled through the context API
    api_key_value = api_key or os.environ.get("HABITIFY_API_KEY")
    if api_key_value:
        try:
            server.metadata = {"api_key": api_key_value}
        except AttributeError:
            # Fallback for new FastMCP API
            server.set_metadata({"api_key": api_key_value})

    @server.tool()
    async def get_habits(include_archived: bool = False) -> dict[str, Any]:
        """
        Get a list of habits.

        Args:
            include_archived: Whether to include archived habits (default: False)

        Returns:
            Dictionary containing the list of habits and count
        """
        return await tools.get_habits(include_archived=include_archived)

    @server.tool()
    async def get_habit(
        id: Optional[str] = None, name: Optional[str] = None
    ) -> dict[str, Any]:
        """
        Get details of a specific habit by ID or name.

        Args:
            id: ID of the habit to retrieve
            name: Name or partial name of the habit to find

        Returns:
            Dictionary containing habit details or error information
        """
        return await tools.get_habit(id=id, name=name)

    @server.tool()
    async def get_habit_status(
        id: Optional[str] = None,
        name: Optional[str] = None,
        date: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Get the status of a habit for one or more dates.

        This tool supports both single date queries and date range queries.

        For a single date, use the 'date' parameter:
            date: Date to check in YYYY-MM-DD format (defaults to today)

        For a date range (all dates in range are inclusive), use one of:
            start_date and end_date: Specific date range
            start_date and days: N days starting from start_date
            end_date and days: N days ending at end_date
            days: N days ending at today

        Args:
            id: ID of the habit to check
            name: Name of the habit to check (alternative to id)
            date: Single date to check in YYYY-MM-DD format
            start_date: Start date for range in YYYY-MM-DD format (inclusive)
            end_date: End date for range in YYYY-MM-DD format (inclusive)
            days: Number of days to include in range

        Returns:
            Dictionary with habit status(es) or error information
        """
        return await tools.get_habit_status(
            id=id,
            name=name,
            date=date,
            start_date=start_date,
            end_date=end_date,
            days=days,
        )

    @server.tool()
    async def set_habit_status(
        id: Optional[str] = None,
        name: Optional[str] = None,
        status: Status = Status.COMPLETED,
        date: Optional[str] = None,
        note: Optional[str] = None,
        value: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Set a habit's status for a specific date.

        Args:
            id: ID of the habit to update
            name: Name of the habit to update (alternative to id)
            status: Status to set: completed, skipped, failed, or none
            date: Date in YYYY-MM-DD format (defaults to today)
            note: Optional note to attach to the log
            value: Optional value for habits with numeric goals

        Returns:
            Dictionary containing status update result or error information
        """
        return await tools.set_habit_status(
            id=id, name=name, status=status.value, date=date, note=note, value=value
        )

    # log_habit tool removed - redundant with set_habit_status
    return server
