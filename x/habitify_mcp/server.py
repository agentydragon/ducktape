"""Habitify MCP Server implementation."""

from datetime import datetime

from fastmcp import Context, FastMCP

from x.habitify_mcp import tools
from x.habitify_mcp.context import CLIENT_KEY, make_lifespan
from x.habitify_mcp.habitify_client import HabitifyClient
from x.habitify_mcp.types import HabitResult, HabitsResult, LogResult, Status, StatusResult


def create_habitify(api_key: str | None = None) -> FastMCP:
    """Create and configure a Habitify MCP server."""
    server = FastMCP(
        "Habitify",
        instructions="Habitify API for habit tracking through Model Context Protocol",
        lifespan=make_lifespan(api_key),
    )

    def get_client(ctx: Context) -> HabitifyClient:
        """Get the HabitifyClient from the lifespan context."""
        client: HabitifyClient = ctx.lifespan_context[CLIENT_KEY]
        return client

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
