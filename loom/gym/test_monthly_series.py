from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import pytest_bazel

from loom.gym.monthly_series import MonthlySeries, add_months, load_series, month_end, validate_known_history

# 2020-05 is a hole (mimics the BLS Oct-2025 CPI gap).
HOLEY = MonthlySeries(
    series_id="holey",
    description="test holey",
    unit="units",
    provenance="synthetic",
    values={date(2020, 3, 1): 104.0, date(2020, 4, 1): 106.0, date(2020, 6, 1): 110.0, date(2020, 7, 1): 112.0},
)


def test_month_arithmetic() -> None:
    assert add_months(date(2024, 11, 1), 2) == date(2025, 1, 1)
    assert month_end(date(2024, 2, 1)) == date(2024, 2, 29)


def test_max_observed_skips_holes() -> None:
    # Window (2020-03, 2020-06] contains the 2020-05 hole; max is over observed months only.
    assert HOLEY.max_observed_between(after=date(2020, 3, 1), through=date(2020, 6, 1)) == 110.0


def test_min_observed_skips_holes() -> None:
    # Window (2020-03, 2020-06] contains the 2020-05 hole; min is over observed months only.
    assert HOLEY.min_observed_between(after=date(2020, 3, 1), through=date(2020, 6, 1)) == 106.0


def test_validate_known_history_rejects_bad_data() -> None:
    good = {date(2024, 11, 1): 6032.38, date(2024, 12, 1): 5881.63}
    validate_known_history("sp500", good)
    with pytest.raises(ValueError, match="bad evidence data"):
        validate_known_history("sp500", good | {date(2024, 12, 1): 1234.0})


def test_load_series_requires_evidence_files(tmp_path: Path) -> None:
    # load_series reads the augur-evidence checkout; a missing file must surface,
    # not silently produce an empty series.
    with pytest.raises(RuntimeError):
        load_series(tmp_path)


if __name__ == "__main__":
    pytest_bazel.main()
