"""Credit accounting: milli-credit integers, streaks, daily bonus.

Credits are tracked as **integer millicredits** (credit value × 1000)
everywhere — Postgres (`balance.credits`, the `credits_*` snapshot columns)
and the wire (`*_millis` fields). No floats: computation uses `Decimal`,
rounded to whole millis on every write; the frontend divides by 1000 only
for display.

Day boundaries are Pacific time regardless of client timezone
(see plans/credit_system_v2.md). Streak and daily-bonus state is
**append-only**: once recorded for a day it is never revoked, even if the
sessions that earned it are later edited or deleted.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from x.study_casino.credit_constants import (
    DAILY_FIRST_BONUS,
    DAILY_STREAK_INCREMENT,
    DAILY_STREAK_STUDY_THRESHOLD_SECONDS,
    REST_DAY_STREAK_INTERVAL,
    STREAK_MULTIPLIER_CAP,
)
from x.study_casino.models import CreditStateRow, SessionRow

PACIFIC = ZoneInfo("America/Los_Angeles")

MILLIS_PER_CREDIT = 1000


def millis_from_credits(amount: Decimal) -> int:
    """Round a decimal credit amount to integer millicredits (half-up)."""
    return int((amount * MILLIS_PER_CREDIT).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def base_session_credits(seconds: int) -> Decimal:
    """1 credit per minute studied, fractional — no floor."""
    return Decimal(seconds) / 60


def streak_multiplier(streak_days: int) -> Decimal:
    """Total multiplier applied to credit awards (1.0 = no bonus)."""
    return 1 + min(streak_days * DAILY_STREAK_INCREMENT, STREAK_MULTIPLIER_CAP)


def streak_bonus_percent(streak_days: int) -> int:
    """Integer wire form of the streak bonus: 1%/day, capped at 100."""
    return int((streak_multiplier(streak_days) - 1) * 100)


def rest_days_available(streak_days: int, rest_days_used: int) -> int:
    return max(0, streak_days // REST_DAY_STREAK_INTERVAL - rest_days_used)


def pacific_date(at_ms: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(at_ms / 1000, tz=PACIFIC).date()


def pacific_day_bounds_ms(day: datetime.date) -> tuple[int, int]:
    """[start, end) of `day` as epoch ms. DST-safe: zoneinfo resolves each
    wall-clock midnight to its own UTC offset."""
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=PACIFIC)
    end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time.min, tzinfo=PACIFIC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def get_or_create_credit_state(s: Session, username: str) -> CreditStateRow:
    row = s.get(CreditStateRow, username)
    if row is None:
        row = CreditStateRow(user_id=username, streak_days=0, rest_days_used=0)
        s.add(row)
        s.flush()
    return row


def qualify_streak_day(state: CreditStateRow, day: datetime.date) -> bool:
    """Advance streak state for the newly-qualified Pacific day `day`.

    Returns whether a rest day was consumed. Idempotent per day, and a
    backdated day earlier than the last qualifying day leaves state alone
    (append-only — no retroactive recalculation).
    """
    last = datetime.date.fromisoformat(state.last_qualifying_date) if state.last_qualifying_date else None
    if last is not None and day <= last:
        return False
    consumed = False
    gap = (day - last).days if last is not None else None
    if gap == 1:
        state.streak_days += 1
    elif gap == 2 and rest_days_available(state.streak_days, state.rest_days_used) > 0:
        # One rest day covers exactly one missed day; larger gaps reset.
        state.rest_days_used += 1
        state.streak_days += 1
        consumed = True
    else:
        state.streak_days = 1
        state.rest_days_used = 0
    state.last_qualifying_date = day.isoformat()
    return consumed


def day_study_seconds(s: Session, username: str, day: datetime.date) -> int:
    start_ms, end_ms = pacific_day_bounds_ms(day)
    return int(
        s.scalar(
            select(func.coalesce(func.sum(SessionRow.seconds), 0)).where(
                SessionRow.user_id == username, SessionRow.ended_at_ms >= start_ms, SessionRow.ended_at_ms < end_ms
            )
        )
    )


@dataclass(frozen=True)
class SessionAward:
    """Breakdown of a live session's credit award. `total_millis` includes the
    (multiplied) daily bonus; the caller credits it to the balance."""

    total_millis: int
    daily_bonus_millis: int
    streak_days: int
    rest_day_consumed: bool


def award_live_session(s: Session, username: str, *, seconds: int, ended_at_ms: int) -> SessionAward:
    """Compute a just-completed live session's credit award and record its
    streak/daily-bonus effects. The session's row must already be flushed so
    the day total includes it.
    """
    state = get_or_create_credit_state(s, username)
    day = pacific_date(ended_at_ms)
    day_iso = day.isoformat()

    rest_day_consumed = False
    bonus = Decimal(0)
    if day_study_seconds(s, username, day) >= DAILY_STREAK_STUDY_THRESHOLD_SECONDS:
        rest_day_consumed = qualify_streak_day(state, day)
        if state.last_first_bonus_date != day_iso:
            bonus = DAILY_FIRST_BONUS
            state.last_first_bonus_date = day_iso

    multiplier = streak_multiplier(state.streak_days)
    return SessionAward(
        total_millis=millis_from_credits((base_session_credits(seconds) + bonus) * multiplier),
        daily_bonus_millis=millis_from_credits(bonus * multiplier),
        streak_days=state.streak_days,
        rest_day_consumed=rest_day_consumed,
    )
