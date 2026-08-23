from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
import pytest_bazel

from aiquota.peak_windows import SCHEDULES, BurnSchedule, PeakPeriod, in_peak, status, upcoming_peaks

_SGT = ZoneInfo("Asia/Singapore")
_WEEKDAYS = frozenset(range(5))


def _schedule(*periods: PeakPeriod, tz: ZoneInfo = _SGT, multiplier: float = 3.0) -> BurnSchedule:
    return BurnSchedule(
        multiplier=multiplier,
        tz=tz,
        periods=periods,
        applies_to="test models",
        source="https://example.invalid/test-schedule",
    )


_AFTERNOON = PeakPeriod(weekdays=_WEEKDAYS, start=time(14, 0), end=time(18, 0))


def _sgt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_SGT)


# 2026-08-24 is a Monday; 2026-08-29 a Saturday.
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (_sgt(2026, 8, 24, 15), True),
        (_sgt(2026, 8, 24, 14), True),  # start is inclusive
        (_sgt(2026, 8, 24, 18), False),  # end is exclusive
        (_sgt(2026, 8, 24, 13, 59), False),
        (_sgt(2026, 8, 29, 15), False),  # Saturday
    ],
)
def test_period_membership_is_start_inclusive_end_exclusive(moment: datetime, expected: bool) -> None:
    assert in_peak(_schedule(_AFTERNOON), moment) is expected


def test_membership_follows_the_schedule_timezone_not_the_callers() -> None:
    # 07:00 UTC is 15:00 in Singapore — inside the window despite the UTC clock
    # reading as morning.
    assert in_peak(_schedule(_AFTERNOON), datetime(2026, 8, 24, 7, 0, tzinfo=UTC)) is True
    assert in_peak(_schedule(_AFTERNOON), datetime(2026, 8, 24, 15, 0, tzinfo=UTC)) is False


def test_next_transition_from_off_peak_is_the_upcoming_start() -> None:
    assert status(_schedule(_AFTERNOON), _sgt(2026, 8, 24, 9)).changes_at == _sgt(2026, 8, 24, 14)


def test_next_transition_from_in_peak_is_the_end() -> None:
    assert status(_schedule(_AFTERNOON), _sgt(2026, 8, 24, 15)).changes_at == _sgt(2026, 8, 24, 18)


def test_next_transition_skips_the_weekend_gap() -> None:
    # Friday evening's next edge is Monday's start, not Saturday's.
    assert status(_schedule(_AFTERNOON), _sgt(2026, 8, 28, 19)).changes_at == _sgt(2026, 8, 31, 14)


def test_wrapped_period_covers_both_sides_of_midnight() -> None:
    overnight = _schedule(PeakPeriod(weekdays=_WEEKDAYS, start=time(22, 0), end=time(2, 0)))
    assert in_peak(overnight, _sgt(2026, 8, 24, 23)) is True
    assert in_peak(overnight, _sgt(2026, 8, 25, 1)) is True  # spilled into Tuesday
    assert in_peak(overnight, _sgt(2026, 8, 25, 3)) is False
    # Saturday 01:00 is still covered — it belongs to Friday's period.
    assert in_peak(overnight, _sgt(2026, 8, 29, 1)) is True
    assert in_peak(overnight, _sgt(2026, 8, 29, 23)) is False


def test_wrapped_period_transition_lands_on_the_following_day() -> None:
    overnight = _schedule(PeakPeriod(weekdays=_WEEKDAYS, start=time(22, 0), end=time(2, 0)))
    assert status(overnight, _sgt(2026, 8, 24, 23)).changes_at == _sgt(2026, 8, 25, 2)


def test_multiple_periods_yield_the_earliest_upcoming_edge() -> None:
    every_day = frozenset(range(7))
    split = _schedule(
        PeakPeriod(weekdays=every_day, start=time(1, 0), end=time(4, 0)),
        PeakPeriod(weekdays=every_day, start=time(6, 0), end=time(10, 0)),
    )
    assert status(split, _sgt(2026, 8, 24, 5)).changes_at == _sgt(2026, 8, 24, 6)
    assert status(split, _sgt(2026, 8, 24, 2)).changes_at == _sgt(2026, 8, 24, 4)


def test_status_reports_unit_multiplier_off_peak_and_schedule_multiplier_in_peak() -> None:
    schedule = _schedule(_AFTERNOON, multiplier=3.0)
    off = status(schedule, _sgt(2026, 8, 24, 9))
    assert (off.in_peak, off.multiplier, off.peak_multiplier) == (False, 1.0, 3.0)
    on = status(schedule, _sgt(2026, 8, 24, 15))
    assert (on.in_peak, on.multiplier, on.peak_multiplier) == (True, 3.0, 3.0)


def test_status_changes_at_is_always_in_the_future_and_flips_the_state() -> None:
    """Holds for every configured schedule, whatever the vendor changes them to."""
    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    for provider, schedule in SCHEDULES.items():
        current = status(schedule, now)
        assert current.changes_at > now, provider
        just_after = current.changes_at.astimezone(UTC)
        assert in_peak(schedule, just_after) is not current.in_peak, provider


def test_schedule_without_periods_is_a_construction_error() -> None:
    with pytest.raises(ValueError, match="no upcoming window"):
        status(_schedule(), datetime(2026, 8, 24, 9, 0, tzinfo=UTC))


def test_upcoming_lists_windows_in_chronological_order_starting_with_the_current_one() -> None:
    during = _sgt(2026, 8, 24, 15)
    windows = upcoming_peaks(_schedule(_AFTERNOON), during, count=3)
    assert [(w.start, w.end) for w in windows] == [
        (_sgt(2026, 8, 24, 14), _sgt(2026, 8, 24, 18)),
        (_sgt(2026, 8, 25, 14), _sgt(2026, 8, 25, 18)),
        (_sgt(2026, 8, 26, 14), _sgt(2026, 8, 26, 18)),
    ]


def test_upcoming_from_off_peak_starts_with_the_next_window_not_a_past_one() -> None:
    windows = upcoming_peaks(_schedule(_AFTERNOON), _sgt(2026, 8, 24, 19), count=2)
    assert [w.start for w in windows] == [_sgt(2026, 8, 25, 14), _sgt(2026, 8, 26, 14)]


def test_upcoming_respects_the_requested_count() -> None:
    assert len(upcoming_peaks(_schedule(_AFTERNOON), _sgt(2026, 8, 24, 9), count=5)) == 5


if __name__ == "__main__":
    pytest_bazel.main()
