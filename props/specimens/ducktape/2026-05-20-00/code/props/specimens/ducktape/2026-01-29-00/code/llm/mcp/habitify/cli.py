"""
Command-line interface for the Habitify MCP server.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

import habitify
from habitify.habitify_client import HabitifyClient, HabitifyError
from habitify.server import create_habitify
from habitify.types import Status
from habitify.utils.cli_utils import (
    format_rich_status,
    get_api_key_from_param_or_env,
    get_status_color,
    resolve_habit_for_cli,
)

# Load environment variables
load_dotenv()

# Set up logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],  # Explicitly use stderr
)
logger = logging.getLogger("habitify")

# Create typer app
app = typer.Typer(help="Habitify CLI and MCP Server")
# Two consoles - one for stderr (server mode) and one for stdout (normal CLI usage)
err_console = Console(stderr=True)  # For server logs and errors
console = Console()  # For normal CLI output


# Signal handling for graceful shutdown
def setup_signal_handlers() -> None:
    """Set up signal handlers for graceful shutdown."""

    def signal_handler(sig, frame):
        logger.info("Received signal to terminate. Shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


@app.command("mcp")
def mcp(
    transport: str = typer.Option("stdio", "--transport", "-t", help="Transport type: stdio or sse"),
    port: int = typer.Option(3000, "--port", "-p", help="Port for SSE transport"),
    api_key: str | None = typer.Option(
        None, "--api-key", "-k", help="Habitify API key (overrides environment variable)"
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable debug output"),
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug logging"),
) -> None:
    """Start the Habitify MCP server with the specified transport."""
    # Configure logging
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    elif quiet:
        logging.getLogger().setLevel(logging.WARNING)
        logger.setLevel(logging.WARNING)

    # Get API key from command line or environment
    habitify_api_key = get_api_key_from_param_or_env(api_key)

    # Check if API key is set
    if not habitify_api_key:
        err_console.print("[bold red]Error:[/] Habitify API key is required.")
        err_console.print("Please set the HABITIFY_API_KEY environment variable or use --api-key.")
        raise typer.Exit(code=1)

    # Set up signal handlers
    setup_signal_handlers()

    # Configure logging level for the server
    server_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if debug:
        server_log_level = "DEBUG"
    elif quiet:
        server_log_level = "WARNING"
    else:
        server_log_level = "INFO"

    # Create server with API key and port configuration
    server = create_habitify(
        debug=debug,
        log_level=server_log_level,
        api_key=habitify_api_key,
        port=port,  # Pass the port parameter here
    )

    # Claude Desktop detection
    is_claude_desktop = not sys.stdin.isatty()

    if is_claude_desktop:
        logger.info("Detected Claude Desktop environment")

    # Run the server
    try:
        if transport == "stdio":
            err_console.print("[bold green]Starting[/] Habitify MCP server with stdio transport")
            server.run(transport="stdio")
        else:
            err_console.print(f"[bold green]Starting[/] Habitify MCP server with SSE transport on port {port}")
            server.run(transport="sse")  # Port is already configured in the server settings
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Keyboard interrupt received.[/] Shutting down...")
    except Exception as e:
        logger.error(f"Error running server: {e}", exc_info=True)
        err_console.print(f"[bold red]Error:[/] {e!s}")
        raise typer.Exit(code=1)


@app.command("install")
def install(
    name: str = typer.Option("Habitify", "--name", "-n", help="Name to register the server as"),
    api_key: str | None = typer.Option(
        None, "--api-key", "-k", help="Habitify API key (overrides environment variable)"
    ),
) -> None:
    """Install the Habitify MCP server to Claude Desktop."""
    # Get API key from command line or environment
    habitify_api_key = get_api_key_from_param_or_env(api_key)

    # Check if API key is set
    if not habitify_api_key:
        err_console.print("[bold red]Error:[/] Habitify API key is required.")
        err_console.print("Please set the HABITIFY_API_KEY environment variable or use --api-key.")
        raise typer.Exit(code=1)

    # Get full path to the server module
    server_path = Path(habitify.__file__).parent
    server_module = f"{server_path}/server.py:create_habitify"

    # Build the command
    cmd = ["mcp", "install", server_module, "--name", name, "-v", f"HABITIFY_API_KEY={habitify_api_key}"]

    # Optional API base URL if set
    api_base_url = os.environ.get("HABITIFY_API_BASE_URL")
    if api_base_url:
        cmd.extend(["-v", f"HABITIFY_API_BASE_URL={api_base_url}"])

    console.print(f"[bold]Installing[/] Habitify MCP server to Claude Desktop as '{name}'...")

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if result.stdout:
            console.print(result.stdout)
        console.print("[bold green]Success![/] Habitify MCP server installed to Claude Desktop.")
        console.print("You can now use Habitify tools in your Claude conversations.")
    except subprocess.CalledProcessError as e:
        err_console.print("[bold red]Error:[/] Failed to install MCP server.")
        if e.stderr:
            err_console.print(e.stderr)
        raise typer.Exit(code=1)


@app.command("list")
def list_habits(
    include_archived: bool = typer.Option(False, "--include-archived", "-a", help="Include archived habits"),
    api_key: str | None = typer.Option(
        None, "--api-key", "-k", help="Habitify API key (overrides environment variable)"
    ),
) -> None:
    """List habits, excluding archived ones by default."""
    # Run the async implementation in an event loop
    asyncio.run(_list_habits_async(include_archived=include_archived, api_key=api_key))


async def _list_habits_async(include_archived: bool = False, api_key: str | None = None) -> None:
    """Async implementation of the list command."""
    # Get API key from command line or environment
    habitify_api_key = get_api_key_from_param_or_env(api_key)

    try:
        # Use async context manager
        async with HabitifyClient(api_key=habitify_api_key) as client:
            habits = await client.get_habits()

            # Filter out archived habits unless include_archived is True
            if not include_archived:
                habits = [h for h in habits if not h.archived]

            table = Table(title="Habitify Habits")
            table.add_column("ID", style="dim")
            table.add_column("Name", style="green")
            table.add_column("Category", style="blue")
            table.add_column("Goal Type", style="yellow")
            table.add_column("Archived", style="red")

            for habit in habits:
                # Access attributes directly since we're using Pydantic models
                table.add_row(
                    habit.id, habit.name, habit.category or "", habit.goal_type or "", "Yes" if habit.archived else "No"
                )

            # Print summary
            if not include_archived:
                console.print(f"Showing {len(habits)} active habits (use --include-archived to show all)")
            else:
                archived_count = sum(1 for h in habits if h.archived)
                console.print(f"Showing all {len(habits)} habits ({archived_count} archived)")

            console.print(table)
    except HabitifyError as e:
        err_console.print(f"[bold red]Error:[/] {e!s}")
        raise typer.Exit(code=1)


@app.command("status")
def status(
    habit: str = typer.Argument(..., help="Habit ID or name"),
    date: str | None = typer.Option(None, "--date", "-d", help="Date in YYYY-MM-DD format (defaults to today)"),
    api_key: str | None = typer.Option(
        None, "--api-key", "-k", help="Habitify API key (overrides environment variable)"
    ),
) -> None:
    """Check a habit's status for a specific date."""
    # Run the async implementation in an event loop
    asyncio.run(_status_async(habit=habit, date=date, api_key=api_key))


async def _status_async(habit: str, date: str | None = None, api_key: str | None = None) -> None:
    """Async implementation of the status command."""
    # Get API key from command line or environment
    habitify_api_key = get_api_key_from_param_or_env(api_key)

    try:
        # Use async context manager
        async with HabitifyClient(api_key=habitify_api_key) as client:
            # Resolve habit ID and name
            habit_id, habit_name = await resolve_habit_for_cli(habit, client, err_console)

            # Check status
            status = await client.check_habit_status(habit_id, date)

            # Create a nice table
            assert status.date is not None, "Status date should not be None"
            formatted_date = status.date.strftime("%B %d, %Y")

            console.print(f"Status for [bold green]{habit_name}[/] on [bold]{formatted_date}[/]:")

            table = Table(show_header=False, box=None)
            table.add_column("Property", style="blue")
            table.add_column("Value", style="green")

            # Format status with color
            status_value = status.status
            status_color = format_rich_status(status_value)

            table.add_row("Status", status_color)
            table.add_row("Date", formatted_date)

            if hasattr(status, "value") and status.value is not None:
                table.add_row("Value", str(status.value))

            if hasattr(status, "note") and status.note:
                table.add_row("Note", status.note)

            console.print(table)

    except HabitifyError as e:
        err_console.print(f"[bold red]Error:[/] {e!s}")
        raise typer.Exit(code=1)


@app.command("log")
def log(
    habit: str = typer.Argument(..., help="Habit ID or name"),
    status: Status = typer.Option(  # noqa: B008
        Status.COMPLETED, "--status", "-s", help="Status to set (completed, skipped, failed, none, in_progress)"
    ),
    date: str | None = typer.Option(None, "--date", "-d", help="Date in YYYY-MM-DD format (defaults to today)"),
    note: str | None = typer.Option(None, "--note", "-n", help="Optional note to attach"),
    value: float | None = typer.Option(None, "--value", "-v", help="Optional value (for number/timer habits)"),
    api_key: str | None = typer.Option(
        None, "--api-key", "-k", help="Habitify API key (overrides environment variable)"
    ),
) -> None:
    """Log a habit with a specific status."""
    # Run the async implementation in an event loop
    asyncio.run(_log_async(habit=habit, status=status, date=date, note=note, value=value, api_key=api_key))


async def _log_async(
    habit: str,
    status: Status,
    date: str | None = None,
    note: str | None = None,
    value: float | None = None,
    api_key: str | None = None,
) -> None:
    """Async implementation of the log command."""
    # Get API key from command line or environment
    habitify_api_key = get_api_key_from_param_or_env(api_key)

    # Validate status
    valid_statuses = ["completed", "skipped", "failed", "none"]
    if status not in valid_statuses:
        console.print(f"[bold red]Error:[/] Invalid status '{status}'")
        console.print(f"Valid statuses: {', '.join(valid_statuses)}")
        raise typer.Exit(code=1)

    try:
        # Use async context manager
        async with HabitifyClient(api_key=habitify_api_key) as client:
            # Resolve habit ID and name
            habit_id, habit_name = await resolve_habit_for_cli(habit, client, err_console)

            # Log the habit
            result = await client.set_habit_status(habit_id=habit_id, status=status, date=date, note=note, value=value)

            # Format date
            if result and result.date:
                formatted_date = result.date.strftime("%B %d, %Y")
            elif date:
                formatted_date = datetime.fromisoformat(date).strftime("%B %d, %Y")
            else:
                formatted_date = datetime.now().strftime("%B %d, %Y")

            # Success message with color based on status
            status_color = get_status_color(status)

            console.print(
                f"Set habit [bold]{habit_name}[/] status to [bold {status_color}]{status}[/] for {formatted_date}"
            )

    except HabitifyError as e:
        err_console.print(f"[bold red]Error:[/] {e!s}")
        raise typer.Exit(code=1)


@app.callback()
def main() -> None:
    """
    Habitify CLI and MCP Server

    Commands:
    - mcp: Start the MCP server
    - install: Install to Claude Desktop
    - list: List all habits
    - status: Check a habit's status
    - log: Log a habit with a specific status
    """


if __name__ == "__main__":
    app()
