"""Behavior tests for augur.dates day-count helpers."""

from __future__ import annotations

import datetime as dt

import pytest_bazel

from augur.dates import DAYS_PER_MONTH, months_between


def test_days_per_month_is_gregorian() -> None:
    assert DAYS_PER_MONTH == 365.2425 / 12


def test_one_calendar_year_is_about_twelve_months() -> None:
    # A 365-day common year is ~11.99 months (the mean Gregorian month is 365.2425/12 days,
    # slightly longer than 365/12), so a calendar year lands within a few hundredths of 12.
    start = dt.date(2021, 1, 1)
    end = dt.date(2022, 1, 1)  # 365 days (2021 is a common year)
    assert abs(months_between(start, end) - 12.0) < 0.05


def test_zero_span_is_zero_months() -> None:
    day = dt.date(2021, 6, 15)
    assert months_between(day, day) == 0.0


def test_reversed_span_is_negative() -> None:
    start = dt.date(2021, 1, 1)
    end = dt.date(2021, 4, 1)
    assert months_between(start, end) > 0.0
    assert months_between(end, start) == -months_between(start, end)


def test_known_day_gap_converts_to_expected_months() -> None:
    # 90-day gap -> 90 / (365.2425/12) months.
    start = dt.date(2022, 1, 1)
    end = start + dt.timedelta(days=90)
    assert months_between(start, end) == 90 / (365.2425 / 12)


if __name__ == "__main__":
    pytest_bazel.main()
