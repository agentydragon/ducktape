from datetime import UTC, date, datetime, timedelta, timezone

import pytest
import pytest_bazel

from cluster.exporters.routeros_log.entries import Clock, parse_clock, parse_timestamp

_CLOCK = Clock(today=date(2026, 10, 10), tz=timezone(timedelta(hours=-7)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01:02:03", datetime(2026, 10, 10, 8, 2, 3, tzinfo=UTC)),
        ("10-09 23:00:00", datetime(2026, 10, 10, 6, 0, 0, tzinfo=UTC)),
        ("oct/09 23:00:00", datetime(2026, 10, 10, 6, 0, 0, tzinfo=UTC)),
        ("2025-12-31 17:00:00", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)),
        ("dec/31/2025 17:00:00", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)),
        # No year, after today: last year's.
        ("12-31 17:00:00", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)),
    ],
)
def test_timestamps_resolve_against_device_clock(value: str, expected: datetime) -> None:
    assert parse_timestamp(value, _CLOCK) == expected


def test_unknown_time_format_raises() -> None:
    with pytest.raises(ValueError, match="unrecognized"):
        parse_timestamp("yesterday", _CLOCK)


@pytest.mark.parametrize("day", ["2026-10-10", "oct/10/2026"])
def test_clock(day: str) -> None:
    assert parse_clock({"date": day, "time": "01:02:03", "gmt-offset": "+05:30"}) == Clock(
        today=date(2026, 10, 10), tz=timezone(timedelta(hours=5, minutes=30))
    )


if __name__ == "__main__":
    pytest_bazel.main()
