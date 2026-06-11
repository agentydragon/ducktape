"""
CLI-specific utility functions.
"""

import typer
from rich.console import Console

from ..habitify_client import HabitifyClient, HabitifyError

# Import asyncio for async functions


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
