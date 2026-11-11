"""Locale-stable presentation helpers for instants and calendar dates."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo

_DATE_ONLY_AFTER = timedelta(days=14)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_clock(value: datetime, tz: tzinfo | None = None) -> str:
    """Format an instant as a local 24-hour clock time."""
    local = value.astimezone(tz)
    return f"{local.hour:02d}:{local.minute:02d}"


def format_weekday(value: datetime, tz: tzinfo | None = None) -> str:
    """Format an instant's weekday in the viewer's local timezone."""
    return _WEEKDAYS[value.astimezone(tz).weekday()]


def format_calendar_date(value: date, *, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    """Format a calendar date without interpreting it as an instant or shifting its day."""
    current = (now or datetime.now().astimezone()).astimezone(tz)
    label = f"{_MONTHS[value.month - 1]} {value.day}"
    if value.year != current.year:
        label += f", {value.year:04d}"
    return label


def format_instant(
    value: datetime | None,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    details: bool = False,
) -> str | None:
    """Show a local date, adding year and clock only when they add context."""
    if value is None:
        return None
    current = (now or datetime.now().astimezone()).astimezone(tz)
    local = value.astimezone(tz)
    if details:
        zone = local.tzname() or ""
        return f"{local.year:04d}-{local.month:02d}-{local.day:02d} {format_clock(local, tz)} {zone}".rstrip()

    label = format_calendar_date(local.date(), now=current, tz=tz)
    if abs(value.astimezone(UTC) - current.astimezone(UTC)) <= _DATE_ONLY_AFTER:
        label += f", {format_clock(local, tz)}"
    return label


def format_peak_interval(start: datetime, end: datetime, tz: tzinfo | None = None) -> str:
    """Format an upcoming window as `Day HH:MM-HH:MM` in the viewer's timezone."""
    return f"{format_weekday(start, tz)} {format_clock(start, tz)}-{format_clock(end, tz)}"
