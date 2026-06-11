from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import pytest_bazel

from finance.evidence.loading import (
    fred_series_frame,
    monthly_last,
    read_monthly_levels,
    source_bytes,
    yahoo_adjusted_close_frame,
)
from finance.evidence.sources import FRED_CPI, YAHOO_BTC, ZILLOW_ZHVI

FRED_TEXT = (
    "observation_date,CPIAUCSL\n"
    "2024-12-30,5900.0\n"
    "2024-12-31,5881.63\n"  # later same-month observation wins after monthly_last
    "2025-10-01,\n"  # empty values (BLS gap style) are dropped
)


def test_fred_frame_parses_and_drops_empty_values() -> None:
    frame = fred_series_frame(FRED_TEXT.encode(), FRED_CPI)
    assert frame["date"].to_list() == [date(2024, 12, 30), date(2024, 12, 31)]
    assert frame["value"].to_list() == [5900.0, 5881.63]


def test_fred_frame_rejects_missing_series_column() -> None:
    with pytest.raises(ValueError, match="observation_date and CPIAUCSL"):
        fred_series_frame(b"observation_date,OTHER\n2024-12-31,1.0\n", FRED_CPI)


def test_monthly_last_takes_last_observation_per_month() -> None:
    monthly = monthly_last(fred_series_frame(FRED_TEXT.encode(), FRED_CPI))
    assert monthly["month"].to_list() == [date(2024, 12, 1)]
    assert monthly["value"].to_list() == [5881.63]


def _yahoo_payload(points: list[tuple[datetime, float | None]]) -> bytes:
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "timestamp": [int(moment.timestamp()) for moment, _ in points],
                        "indicators": {"adjclose": [{"adjclose": [value for _, value in points]}]},
                    }
                ]
            }
        }
    ).encode()


def test_yahoo_frame_skips_missing_closes_and_enforces_minimum_samples() -> None:
    points = [
        (datetime(2024, 12, 2, tzinfo=UTC), 95000.0),
        (datetime(2024, 12, 30, tzinfo=UTC), 93429.0),
        (datetime(2025, 1, 6, tzinfo=UTC), None),
        (datetime(2025, 1, 13, tzinfo=UTC), 94000.0),
    ]
    frame = yahoo_adjusted_close_frame(_yahoo_payload(points), YAHOO_BTC, minimum_samples=2)
    assert frame["value"].to_list() == [95000.0, 93429.0, 94000.0]
    with pytest.raises(ValueError, match="credible adjusted-close history"):
        yahoo_adjusted_close_frame(_yahoo_payload(points), YAHOO_BTC, minimum_samples=10)


def test_read_monthly_levels_from_checkout_dir(tmp_path: Path) -> None:
    (tmp_path / FRED_CPI.output_filename).write_text(FRED_TEXT)
    levels = read_monthly_levels(tmp_path, FRED_CPI)
    assert [(level.month, level.value) for level in levels] == [(date(2024, 12, 1), 5881.63)]


def test_read_monthly_levels_rejects_zillow() -> None:
    with pytest.raises(ValueError, match="wide city table"):
        read_monthly_levels(Path("/nonexistent"), ZILLOW_ZHVI)


def test_source_bytes_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="evidence not found"):
        source_bytes(tmp_path, FRED_CPI)


if __name__ == "__main__":
    pytest_bazel.main()
