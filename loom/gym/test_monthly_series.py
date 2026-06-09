from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import pytest_bazel

from loom.gym.monthly_series import load_series, monthly_from_fred_csv, monthly_from_yahoo_chart, validate_known_history


def test_fred_parse_takes_last_observation_per_month() -> None:
    text = (
        "observation_date,SP500\n"
        "2012-12-31,1400.0\n"  # pre-window history is dropped
        "2024-12-30,5900.0\n"
        "2024-12-31,5881.63\n"  # later same-month observation wins
        "2025-10-01,\n"  # empty values (BLS gap style) are skipped
    )
    assert monthly_from_fred_csv(text, "SP500") == {date(2024, 12, 1): 5881.63}


def test_yahoo_parse_handles_coarse_bars_and_gaps() -> None:
    # Yahoo may serve weekly/monthly bars under range=max; the parser keeps the
    # last positive observation per month at whatever granularity it gets.
    points = [
        (datetime(2024, 12, 2, tzinfo=UTC), 95000.004),
        (datetime(2024, 12, 30, tzinfo=UTC), 93429.0),
        (datetime(2025, 1, 6, tzinfo=UTC), None),
        (datetime(2025, 1, 13, tzinfo=UTC), 94000.0),
    ]
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [int(moment.timestamp()) for moment, _ in points],
                    "indicators": {"adjclose": [{"adjclose": [value for _, value in points]}]},
                }
            ]
        }
    }
    assert monthly_from_yahoo_chart(payload) == {date(2024, 12, 1): 93429.0, date(2025, 1, 1): 94000.0}


def test_validate_known_history_rejects_bad_data() -> None:
    good = {date(2024, 11, 1): 6032.38, date(2024, 12, 1): 5881.63}
    validate_known_history("sp500", good)
    with pytest.raises(ValueError, match="bad evidence data"):
        validate_known_history("sp500", good | {date(2024, 12, 1): 1234.0})


def test_load_series_requires_real_files(tmp_path: Path) -> None:
    # load_series reads the augur-evidence checkout; a missing file must surface,
    # not silently produce an empty series.
    with pytest.raises(FileNotFoundError):
        load_series(tmp_path)


if __name__ == "__main__":
    pytest_bazel.main()
