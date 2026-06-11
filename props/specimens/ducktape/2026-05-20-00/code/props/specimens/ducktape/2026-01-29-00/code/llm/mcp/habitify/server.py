"""Habitify MCP Server implementation."""

from datetime import datetime
from typing import Literal, cast

from mcp.server.fastmcp import Context, FastMCP

from habitify import tools
from habitify.context import make_lifespan
from habitify.habitify_client import HabitifyClient
from habitify.types import HabitResult, HabitsResult, LogResult, Status, StatusResult


def create_habitify(
    debug: bool = False,
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
    api_key: str | None = None,
    port: int = 3000,
) -> FastMCP:
    """Create and configure a Habitify MCP server."""
    server = FastMCP(
        "Habitify",
        instructions="Habitify API for habit tracking through Model Context Protocol",
        dependencies=["httpx", "python-dotenv"],
        debug=debug,
        log_level=log_level,
        port=port,
        lifespan=make_lifespan(api_key),
    )

    def get_client(ctx: Context) -> HabitifyClient:
        """Get the HabitifyClient from the lifespan context."""
        return cast(HabitifyClient, ctx.request_context.lifespan_context)

    @server.tool()
    async def get_habits(ctx: Context, include_archived: bool = False) -> HabitsResult:
        return await tools.get_habits(get_client(ctx), include_archived=include_archived)

    @server.tool()
    async def get_habit(ctx: Context, id: str | None = None, name: str | None = None) -> HabitResult:
        return await tools.get_habit(get_client(ctx), id=id, name=name)

    @server.tool()
    async def get_habit_status(
        ctx: Context, id: str | None = None, name: str | None = None, date: datetime | None = None
    ) -> StatusResult:
        """Get habit status for a single date (defaults to today)."""
        return await tools.get_habit_status(get_client(ctx), id=id, name=name, date=date)

    @server.tool()
    async def set_habit_status(
        ctx: Context,
        id: str | None = None,
        name: str | None = None,
        status: Status = Status.COMPLETED,
        date: datetime | None = None,
        note: str | None = None,
        value: float | None = None,
    ) -> LogResult:
        """Set habit status for a specific date (defaults to today)."""
        return await tools.set_habit_status(
            get_client(ctx), id=id, name=name, status=status, date=date, note=note, value=value
        )

    return server
