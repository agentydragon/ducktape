"""Calendar-month helpers shared by finance consumers."""

from __future__ import annotations

from datetime import date


def offset_calendar_month(month: date, offset: int) -> date:
    """Move a first-of-month date by a calendar-month offset."""

    absolute_month = month.year * 12 + month.month - 1 + offset
    year, zero_based_month = divmod(absolute_month, 12)
    return date(year, zero_based_month + 1, 1)


def is_next_calendar_month(earlier: date, later: date) -> bool:
    """Whether two monthly observations are exactly one calendar month apart."""

    return offset_calendar_month(earlier, 1) == later
