from __future__ import annotations

import datetime
from datetime import UTC, date

import pytest
import pytest_bazel

from loom.gym.fetch_series import (
    KNOWN_HISTORY,
    monthly_from_fred_csv,
    monthly_from_yahoo_chart,
    render_csv,
    validate_known_history,
)
from loom.gym.monthly_series import parse_monthly_csv


def _chart(granularity: str, points: list[tuple[datetime.datetime, float | None]]) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"dataGranularity": granularity},
                    "timestamp": [int(moment.timestamp()) for moment, _ in points],
                    "indicators": {"quote": [{"close": [close for _, close in points]}]},
                }
            ]
        }
    }


def test_yahoo_monthly_takes_last_close_and_drops_current_month() -> None:
    chart = _chart(
        "1d",
        [
            (datetime.datetime(2024, 1, 15, tzinfo=UTC), 1.111),
            (datetime.datetime(2024, 1, 30, tzinfo=UTC), None),  # missing closes are skipped
            (datetime.datetime(2024, 1, 31, tzinfo=UTC), 2.225),
            (datetime.datetime(2024, 2, 5, tzinfo=UTC), 3.0),  # current (partial) month
        ],
    )
    assert monthly_from_yahoo_chart(chart, current_month=date(2024, 2, 1)) == {date(2024, 1, 1): 2.23}


def test_yahoo_rejects_degraded_granularity() -> None:
    # range=max silently degrades ^GSPC to 3mo bars; the fetcher must refuse them.
    with pytest.raises(ValueError, match="daily bars"):
        monthly_from_yahoo_chart(_chart("3mo", []), current_month=date(2024, 2, 1))


def test_fred_parse_skips_empty_values_and_old_history() -> None:
    text = "observation_date,CPIAUCSL\n2012-12-01,200.0\n2025-09-01,324.368\n2025-10-01,\n2025-11-01,325.0\n"
    # 2025-10 is the BLS shutdown gap (empty value); 2012 predates the history window.
    assert monthly_from_fred_csv(text) == {date(2025, 9, 1): 324.368, date(2025, 11, 1): 325.0}


def test_render_round_trips_through_loader() -> None:
    values = {date(2024, 1, 1): 5881.63, date(2024, 2, 1): 93429.2}
    assert parse_monthly_csv(render_csv(values, "provenance note")) == values


def test_validate_known_history_rejects_bad_fetch() -> None:
    good = {known.month: known.value for known in KNOWN_HISTORY if known.series_id == "sp500"}
    validate_known_history("sp500", good)
    bad = good | {date(2024, 12, 1): 1234.0}
    with pytest.raises(ValueError, match="bad fetch"):
        validate_known_history("sp500", bad)


if __name__ == "__main__":
    pytest_bazel.main()
