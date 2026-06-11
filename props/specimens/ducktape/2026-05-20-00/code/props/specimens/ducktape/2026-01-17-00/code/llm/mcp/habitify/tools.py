"""Habitify MCP tools implementation."""

from datetime import datetime

from .habitify_client import HabitifyClient, HabitifyError
from .types import (
    DateRangeStatusItem,
    DateRangeStatusResult,
    HabitResult,
    HabitsResult,
    LogResult,
    Status,
    StatusResult,
)
from .utils.habit_resolver import resolve_habit


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
    client: HabitifyClient,
    *,
    id: str | None = None,
    name: str | None = None,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    days: int | None = None,
) -> StatusResult | DateRangeStatusResult:
    """Get a habit's status for one or more dates.

    Single date: use 'date' parameter.
    Date range (inclusive): use start_date/end_date, start_date/days, end_date/days, or just days.
    """
    _require_habit_identifier(id=id, name=name, action="check")
    resolved = await resolve_habit(client, id=id, name=name)

    is_range_query = any((start_date, end_date, days))

    if date and is_range_query:
        raise HabitifyError("Cannot specify both date and date range parameters (start_date, end_date, days).")

    if is_range_query:
        statuses = await client.check_habit_status_range(
            resolved.habit_id, start_date=start_date, end_date=end_date, days=days
        )

        items = []
        first_date = None
        last_date = None

        for status in statuses:
            # TODO: Verify if status.date can actually be None in API responses
            assert status.date is not None, "Status date should not be None in range query"
            items.append(DateRangeStatusItem(date=status.date, status=status.status))
            if first_date is None or status.date < first_date:
                first_date = status.date
            if last_date is None or status.date > last_date:
                last_date = status.date

        return DateRangeStatusResult(
            statuses=items,
            start_date=first_date or datetime.now().strftime("%Y-%m-%d"),
            end_date=last_date or datetime.now().strftime("%Y-%m-%d"),
            date_count=len(items),
        )

    date_str = date or datetime.now().strftime("%Y-%m-%d")
    status = await client.check_habit_status(resolved.habit_id, date_str)
    return StatusResult(status=status.status, date=date_str)


async def set_habit_status(
    client: HabitifyClient,
    *,
    id: str | None = None,
    name: str | None = None,
    status: Status = Status.COMPLETED,
    date: str | None = None,
    note: str | None = None,
    value: float | None = None,
) -> LogResult:
    """Set a habit's status for a specific date."""
    _require_habit_identifier(id=id, name=name, action="set")
    resolved = await resolve_habit(client, id=id, name=name)

    result = await client.set_habit_status(resolved.habit_id, status, date, note, value)
    # TODO: Verify if result.date can actually be None when setting status
    assert result.date is not None, "Result date should not be None after setting status"
    return LogResult(status=result.status, date=result.date, note=result.note, value=result.value)
