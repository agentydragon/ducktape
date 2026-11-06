"""
Date utility functions for Habitify.
Provides consistent date handling across CLI and API components.
"""

import datetime


def parse_date(date_string: str | None = None) -> datetime.datetime:
    """Parse an ISO date/datetime or YYYY-MM-DD, defaulting to local now for missing or empty input."""
    if not date_string:
        return datetime.datetime.now()

    try:
        return datetime.datetime.fromisoformat(date_string)
    except ValueError:
        try:
            # Try to parse as YYYY-MM-DD
            return datetime.datetime.strptime(date_string, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid date format: {date_string}. Please use YYYY-MM-DD.")


def _normalize_date(date: str | datetime.date | datetime.datetime | None = None) -> datetime.datetime:
    """Keep datetimes, turn dates into midnight, parse strings, or default None to local now."""
    if date is None:
        return datetime.datetime.now()

    if isinstance(date, datetime.datetime):
        return date

    if isinstance(date, datetime.date):
        # Convert date to datetime
        return datetime.datetime.combine(date, datetime.datetime.min.time())

    return parse_date(date)


def format_date_for_api(date: str | datetime.date | datetime.datetime | None = None) -> str:
    """Date-only becomes midnight +00:00; ISO strings with Z/+ keep offsets (Z -> +00:00); other values receive +00:00."""
    # Special case for YYYY-MM-DD string format - optimize to avoid parsing
    if isinstance(date, str) and len(date.split("-")) == 3:
        try:
            datetime.datetime.strptime(date, "%Y-%m-%d")
            return f"{date}T00:00:00+00:00"
        except ValueError:
            pass

    # If already in ISO format with timezone, return as is
    if isinstance(date, str) and "T" in date and ("Z" in date or "+" in date):
        return date.replace("Z", "+00:00")

    # Special case for date objects
    if isinstance(date, datetime.date) and not isinstance(date, datetime.datetime):
        return f"{date.isoformat()}T00:00:00+00:00"

    return _normalize_date(date).strftime("%Y-%m-%dT%H:%M:%S+00:00")
