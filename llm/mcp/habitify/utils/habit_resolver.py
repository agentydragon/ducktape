"""Utility to resolve a habit by name or ID."""

from habitify.habitify_client import HabitifyClient, HabitifyError
from habitify.types import ResolvedHabit


async def resolve_habit(client: HabitifyClient, id: str | None = None, name: str | None = None) -> ResolvedHabit:
    """Resolve a habit by name or ID.

    Raises HabitifyError if habit cannot be resolved.
    """
    if id:
        if not isinstance(id, str):
            msg = "Habit ID must be a string."
            raise HabitifyError(msg)
        return ResolvedHabit(habit_id=id if id.startswith("-") else f"-{id}")

    if not name:
        msg = "Either id or name must be provided."
        raise HabitifyError(msg)

    habits = await client.get_habits()
    habit_name = name.lower().strip()
    matching_habits = [h for h in habits if habit_name in h.name.lower()]

    if not matching_habits:
        msg = f'No habit found with name containing "{name}"'
        raise HabitifyError(msg)

    if len(matching_habits) > 1:
        exact_match = next((h for h in matching_habits if h.name.lower() == habit_name), None)
        if exact_match:
            return ResolvedHabit(habit_id=exact_match.id, habit_name=exact_match.name, match_type="exact")

        matches = ", ".join(f"{h.name} ({h.id})" for h in matching_habits[:5])
        msg = f'Multiple habits found matching "{name}": {matches}'
        raise HabitifyError(msg)

    return ResolvedHabit(
        habit_id=matching_habits[0].id,
        habit_name=matching_habits[0].name,
        match_type=("exact" if matching_habits[0].name.lower() == habit_name else "partial"),
    )
