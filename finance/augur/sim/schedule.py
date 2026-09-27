"""When a standing cashflow or a bill falls due."""

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class Once:
    month: int


@dataclass(frozen=True, kw_only=True)
class Recurring:
    """Every month from `start_month` through `end_month`; a `None` end runs to the horizon."""

    start_month: int
    end_month: int | None


type Schedule = Once | Recurring


def is_due(schedule: Schedule, month: int) -> bool:
    if isinstance(schedule, Once):
        return schedule.month == month
    return schedule.start_month <= month and (schedule.end_month is None or month <= schedule.end_month)
