"""
CLI-specific utility functions.
"""

import typer
from rich.console import Console

from habitify.config import load_api_key
from habitify.habitify_client import HabitifyClient, HabitifyError

STATUS_COLORS = {"completed": "green", "skipped": "yellow", "failed": "red", "none": "blue"}


def get_status_color(status: str) -> str:
    """Get the color code for a habit status."""
    return STATUS_COLORS.get(status.lower(), "white")


def format_rich_status(status: str) -> str:
    """Format a status string with Rich formatting."""
    color = get_status_color(status)
    return f"[{color}]{status.capitalize()}[/]"


def get_api_key_from_param_or_env(api_key_param: str | None = None) -> str | None:
    """Get API key from CLI parameter or environment."""
    return api_key_param or load_api_key(exit_on_missing=False)


async def resolve_habit_for_cli(habit: str, client: HabitifyClient, err_console: Console) -> tuple[str, str]:
    """
    Resolve a habit by ID or name for CLI commands.

    Args:
        habit: Habit ID or name
        client: HabitifyClient instance
        err_console: Console for error output

    Returns:
        Tuple of (habit_id, habit_name)

    Raises:
        typer.Exit: If habit can't be resolved
    """
    # Check if habit is an ID
    is_id = habit.startswith("-") or (habit.isalnum() and len(habit) > 8)

    # Either get by ID or search for name
    if is_id:
        try:
            habit_obj = await client.get_habit(habit)
            return habit_obj.id, habit_obj.name
        except HabitifyError as e:
            err_console.print(f"[bold red]Error:[/] {e!s}")
            raise typer.Exit(code=1)
    else:
        # Get all habits and filter by name
        try:
            habits = await client.get_habits()
            matching_habits = [h for h in habits if habit.lower() in h.name.lower()]

            if not matching_habits:
                err_console.print(f"[bold red]Error:[/] No habits found matching '{habit}'")
                raise typer.Exit(code=1)

            if len(matching_habits) > 1:
                # If multiple matches, show them and ask for ID instead
                err_console.print(f"[yellow]Multiple habits match '{habit}':[/]")
                for h in matching_habits:
                    err_console.print(f"  {h.id}: {h.name}")
                err_console.print("\nPlease use the exact habit ID instead.")
                raise typer.Exit(code=1)

            habit_data = matching_habits[0]
            return habit_data.id, habit_data.name
        except HabitifyError as e:
            err_console.print(f"[bold red]Error:[/] {e!s}")
            raise typer.Exit(code=1)
