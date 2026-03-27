"""Habitify MCP tools implementation."""

from datetime import UTC, datetime

from habitify.habitify_client import HabitifyClient, HabitifyError
from habitify.types import HabitResult, HabitsResult, LogResult, Status, StatusResult
from habitify.utils.habit_resolver import resolve_habit


def _require_habit_identifier(*, id: str | None, name: str | None, action: str = "use") -> None:
    """Validate that either an ID or name is provided."""
    if not id and not name:
        raise HabitifyError(f"Either a habit ID or habit name is required to {action} a habit.")


async def get_habits(client: HabitifyClient, *, include_archived: bool = False) -> HabitsResult:
    """Get all habits."""
    habits = await client.get_habits()
    if not include_archived:
        habits = [habit for habit in habits if not habit.archived]
    return HabitsResult(habits=habits, count=len(habits))


async def get_habit(client: HabitifyClient, *, id: str | None = None, name: str | None = None) -> HabitResult:
    """Get a specific habit by ID or name."""
    _require_habit_identifier(id=id, name=name, action="get")

    if id:
        habit = await client.get_habit(id)
        return HabitResult(habit=habit)

    # Resolve by name
    habits = await client.get_habits()
    habit_name = name.lower().strip()  # type: ignore[union-attr]
    matching_habits = [h for h in habits if habit_name in h.name.lower()]

    if not matching_habits:
        raise HabitifyError(f'No habit found with name containing "{name}"')

    exact_match = next((h for h in matching_habits if h.name.lower() == habit_name), None)
    if exact_match:
        return HabitResult(habit=exact_match, match_type="exact")

    if len(matching_habits) == 1:
        return HabitResult(habit=matching_habits[0], match_type="partial")

    matches = ", ".join(f"{h.name} ({h.id})" for h in matching_habits[:5])
    raise HabitifyError(f'Multiple habits found matching "{name}": {matches}')


async def get_habit_status(
    client: HabitifyClient, *, id: str | None = None, name: str | None = None, date: datetime | None = None
) -> StatusResult:
    """Get a habit's status for a single date.

    Args:
        client: HabitifyClient instance
        id: Habit ID (optional if name is provided)
        name: Habit name (optional if id is provided)
        date: Date to check (defaults to today)

    Returns:
        StatusResult with status and date
    """
    _require_habit_identifier(id=id, name=name, action="check")
    resolved = await resolve_habit(client, id=id, name=name)

    check_date = date or datetime.now(tz=UTC)
    status = await client.check_habit_status(resolved.habit_id, check_date)
    assert status.date is not None, "Status date should not be None"
    return StatusResult(status=status.status, date=status.date)


async def set_habit_status(
    client: HabitifyClient,
    *,
    id: str | None = None,
    name: str | None = None,
    status: Status = Status.COMPLETED,
    date: datetime | None = None,
    note: str | None = None,
    value: float | None = None,
) -> LogResult:
    """Set a habit's status for a specific date.

    Args:
        client: HabitifyClient instance
        id: Habit ID (optional if name is provided)
        name: Habit name (optional if id is provided)
        status: Status to set (defaults to COMPLETED)
        date: Date to set status for (defaults to today)
        note: Optional note to attach
        value: Optional value for habits with goals

    Returns:
        LogResult with status, date, note, and value
    """
    _require_habit_identifier(id=id, name=name, action="set")
    resolved = await resolve_habit(client, id=id, name=name)

    set_date = date or datetime.now(tz=UTC)
    result = await client.set_habit_status(resolved.habit_id, status, set_date, note, value)
    assert result.date is not None, "Result date should not be None after setting status"
    return LogResult(status=result.status, date=result.date, note=result.note, value=result.value)
