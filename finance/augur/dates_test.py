"""Behavior tests for augur.dates day-count helpers."""

from __future__ import annotations

import datetime as dt

import pytest_bazel

from finance.augur.dates import months_between


def test_one_calendar_year_is_about_twelve_months() -> None:
    # A 365-day common year is ~11.99 months (the mean Gregorian month is 365.2425/12 days,
    # slightly longer than 365/12), so a calendar year lands within a few hundredths of 12.
    start = dt.date(2021, 1, 1)
    end = dt.date(2022, 1, 1)  # 365 days (2021 is a common year)
    assert abs(months_between(start, end) - 12.0) < 0.05


def test_reversed_span_is_negative() -> None:
    start = dt.date(2021, 1, 1)
    end = dt.date(2021, 4, 1)
    assert months_between(start, end) > 0.0
    assert months_between(end, start) == -months_between(start, end)


if __name__ == "__main__":
    pytest_bazel.main()
