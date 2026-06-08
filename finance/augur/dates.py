"""Calendar day-count helpers: convert wall-clock date spans to fractional model months/years."""

from __future__ import annotations

import datetime as dt

# Mean Gregorian year (365.2425 days) split into 12 months. The canonical day-count for
# converting observation date-gaps into fractional model months across augur.
DAYS_PER_MONTH = 365.2425 / 12.0


def months_between(start: dt.date, end: dt.date) -> float:
    """Fractional months from `start` to `end` (may be negative). Caller floors/guards as needed."""
    return (end - start).days / DAYS_PER_MONTH
