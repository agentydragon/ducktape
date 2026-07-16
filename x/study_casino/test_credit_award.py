"""Pure-logic tests for credit_award: milli rounding, multiplier, streak state."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest_bazel

from x.study_casino.credit_award import (
    base_session_credits,
    millis_from_credits,
    pacific_date,
    pacific_day_bounds_ms,
    pending_streak_days,
    qualify_streak_day,
    rest_days_available,
    streak_bonus_percent,
    streak_multiplier,
)
from x.study_casino.models import CreditStateRow


def _state(streak_days: int = 0, last: str | None = None, rest_used: int = 0) -> CreditStateRow:
    return CreditStateRow(user_id="u", streak_days=streak_days, last_qualifying_date=last, rest_days_used=rest_used)


def _day(iso: str) -> datetime.date:
    return datetime.date.fromisoformat(iso)


# ── Millicredit accounting ───────────────────────────────────────────────────


def test_millis_rounding_has_no_drift_at_minute_boundary() -> None:
    # 61 s → 1.01667 credits → 1017 millis; 59 s → 0.98333 → 983. Sum exact.
    assert millis_from_credits(base_session_credits(61)) == 1017
    assert millis_from_credits(base_session_credits(59)) == 983
    assert 1017 + 983 == 2000


def test_whole_minutes_stay_exact() -> None:
    assert millis_from_credits(base_session_credits(25 * 60)) == 25000


# ── Multiplier ───────────────────────────────────────────────────────────────


def test_streak_multiplier_ramp_and_cap() -> None:
    assert streak_multiplier(0) == Decimal("1.00")
    assert streak_multiplier(7) == Decimal("1.07")
    assert streak_multiplier(50) == Decimal("1.50")
    assert streak_multiplier(100) == Decimal("2.00")
    assert streak_multiplier(250) == Decimal("2.00")  # capped


def test_streak_bonus_percent_is_exact_integer() -> None:
    assert streak_bonus_percent(0) == 0
    assert streak_bonus_percent(7) == 7
    assert streak_bonus_percent(250) == 100  # capped


def test_rest_days_accrue_every_14_streak_days() -> None:
    assert rest_days_available(13, 0) == 0
    assert rest_days_available(14, 0) == 1
    assert rest_days_available(28, 1) == 1
    assert rest_days_available(28, 2) == 0


# ── Streak qualification ─────────────────────────────────────────────────────


def test_first_qualifying_day_starts_streak() -> None:
    state = _state()
    assert qualify_streak_day(state, _day("2026-07-01")) is False
    assert state.streak_days == 1
    assert state.last_qualifying_date == "2026-07-01"


def test_consecutive_day_increments() -> None:
    state = _state(streak_days=6, last="2026-07-01")
    qualify_streak_day(state, _day("2026-07-02"))
    assert state.streak_days == 7


def test_same_day_is_idempotent_and_backdated_day_is_ignored() -> None:
    state = _state(streak_days=6, last="2026-07-01", rest_used=0)
    qualify_streak_day(state, _day("2026-07-01"))
    qualify_streak_day(state, _day("2026-06-25"))
    assert state.streak_days == 6
    assert state.last_qualifying_date == "2026-07-01"


def test_one_day_gap_consumes_rest_day() -> None:
    state = _state(streak_days=15, last="2026-07-01")
    assert qualify_streak_day(state, _day("2026-07-03")) is True
    assert state.streak_days == 16
    assert state.rest_days_used == 1


def test_one_day_gap_without_rest_day_resets() -> None:
    state = _state(streak_days=5, last="2026-07-01")
    assert qualify_streak_day(state, _day("2026-07-03")) is False
    assert state.streak_days == 1
    assert state.rest_days_used == 0


def test_two_day_gap_resets_even_with_rest_day_banked() -> None:
    # Rest days cover exactly one missed day (plan scenario 23).
    state = _state(streak_days=15, last="2026-07-01")
    qualify_streak_day(state, _day("2026-07-04"))
    assert state.streak_days == 1
    assert state.rest_days_used == 0


def test_reset_restores_rest_day_budget() -> None:
    # rest_days_used resets with the streak, so a new streak re-earns rest days.
    state = _state(streak_days=20, last="2026-07-01", rest_used=1)
    qualify_streak_day(state, _day("2026-07-10"))
    assert state.streak_days == 1
    assert rest_days_available(state.streak_days, state.rest_days_used) == 0


def test_pending_streak_days_mirrors_qualification_without_mutating() -> None:
    assert pending_streak_days(_state(), _day("2026-07-01")) == 1  # first ever day
    assert pending_streak_days(_state(streak_days=6, last="2026-07-01"), _day("2026-07-01")) == 6  # already qualified
    assert pending_streak_days(_state(streak_days=6, last="2026-07-01"), _day("2026-07-02")) == 7  # consecutive
    assert pending_streak_days(_state(streak_days=15, last="2026-07-01"), _day("2026-07-03")) == 16  # rest day covers
    assert pending_streak_days(_state(streak_days=5, last="2026-07-01"), _day("2026-07-03")) == 1  # gap resets

    state = _state(streak_days=6, last="2026-07-01")
    pending_streak_days(state, _day("2026-07-02"))
    assert state.streak_days == 6  # projection never mutates


# ── Pacific day handling ─────────────────────────────────────────────────────


def test_pacific_date_uses_la_wall_clock() -> None:
    # 2026-07-16 03:00 UTC is still 2026-07-15 in LA (UTC-7 in July).
    at_ms = int(datetime.datetime(2026, 7, 16, 3, 0, tzinfo=datetime.UTC).timestamp() * 1000)
    assert pacific_date(at_ms) == _day("2026-07-15")


def test_pacific_day_bounds_cover_dst_transition() -> None:
    # 2026-03-08 is the spring-forward day in LA: only 23 hours long.
    start_ms, end_ms = pacific_day_bounds_ms(_day("2026-03-08"))
    assert (end_ms - start_ms) == 23 * 3600 * 1000


if __name__ == "__main__":
    pytest_bazel.main()
