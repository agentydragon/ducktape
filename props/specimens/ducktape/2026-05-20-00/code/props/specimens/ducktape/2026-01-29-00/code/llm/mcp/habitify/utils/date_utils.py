"""
Date utility functions for Habitify.
Provides consistent date handling across CLI and API components.
"""

import datetime
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def parse_date(date_string: str | None = None) -> datetime.datetime:
    """
    Parse a date string to a datetime object.

    Args:
        date_string: Date string in various formats

    Returns:
        datetime object
    """
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
    """
    Convert various date formats to a datetime object.

    Args:
        date: Date to normalize (default: current date)

    Returns:
        Normalized datetime object
    """
    if date is None:
        return datetime.datetime.now()

    if isinstance(date, datetime.datetime):
        return date

    if isinstance(date, datetime.date):
        # Convert date to datetime
        return datetime.datetime.combine(date, datetime.datetime.min.time())

    return parse_date(date)


def _format_with_template[T](
    date: str | datetime.date | datetime.datetime | None, formatter: Callable[[datetime.datetime], T]
) -> T:
    """
    Format a date using a formatter function.

    Args:
        date: Date to format (default: current date)
        formatter: Formatting function that takes a datetime and returns formatted value

    Returns:
        Formatted date according to the formatter function
    """
    normalized_date = _normalize_date(date)
    return formatter(normalized_date)


def format_date_yyyy_mm_dd(date: str | datetime.date | datetime.datetime | None = None) -> str:
    """
    Format a date as YYYY-MM-DD.

    Args:
        date: Date to format (default: current date)

    Returns:
        Date in YYYY-MM-DD format
    """
    return _format_with_template(date, lambda d: d.strftime("%Y-%m-%d"))


def format_date_for_api(date: str | datetime.date | datetime.datetime | None = None) -> str:
    """
    Format a date for the Habitify API (ISO format with timezone).

    Args:
        date: Date to format (default: current date)

    Returns:
        Date in ISO format with timezone
    """
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

    # Use the common formatter for other cases
    return _format_with_template(date, lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+00:00"))


def format_date_human(date: str | datetime.date | datetime.datetime | None = None) -> str:
    """
    Get a human-readable date format.

    Args:
        date: Date to format

    Returns:
        Human-readable date (e.g., "Jan 15, 2023")
    """
    return _format_with_template(date, lambda d: d.strftime("%b %d, %Y"))


def validate_date_format(date_str: str) -> bool:
    """
    Validate that a date string has a recognized format.

    Args:
        date_str: Date string to validate

    Returns:
        True if the date has a valid format, False otherwise
    """
    try:
        parse_date(date_str)
        return True
    except ValueError:
        return False
