from __future__ import annotations

from datetime import date

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.series import SP500_KEY
from finance.augur.x.models.state_space_fit import _historical_cumulative_return


def _historical_series(months: tuple[str, ...], levels: tuple[float, ...]) -> HistoricalSeries:
    return HistoricalSeries(
        series_names=(SP500_KEY,), levels=np.asarray(levels, dtype=np.float64)[:, None], months=months
    )


def test_sp500_cumulative_return_uses_exact_month_endpoints() -> None:
    historical = _historical_series(
        ("2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06"), (100.0, 110.0, 105.0, 120.0, 125.0, 140.0)
    )

    result = _historical_cumulative_return(historical, SP500_KEY.wire_id, date(2024, 2, 12), date(2024, 5, 28))

    assert result == pytest.approx(np.log(125.0 / 110.0))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2024, 2, 1), date(2024, 5, 31)),
        (date(2023, 12, 1), date(2024, 2, 1)),
        (date(2024, 5, 1), date(2024, 7, 1)),
    ],
)
def test_sp500_cumulative_return_rejects_incomplete_interval_coverage(start: date, end: date) -> None:
    historical = _historical_series(
        ("2024-01", "2024-02", "2024-04", "2024-05", "2024-06"), (100.0, 110.0, 120.0, 125.0, 140.0)
    )

    assert _historical_cumulative_return(historical, SP500_KEY.wire_id, start, end) is None


if __name__ == "__main__":
    pytest_bazel.main()
