"""Exact amount quotes use only the selected path and declared reset boundaries."""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import MAX_COUNT
from finance.augur.sim.prepared import PreparedFixedAmount, PreparedIndexedAmount, PreparedSeries


@pytest.fixture
def inflation() -> PreparedSeries:
    """Two paths of six snapshots."""
    return PreparedSeries(series_id="inflation", snapshots=6, values=(2, 3, 5, 7, 11, 13, 4, 1, 2, 99, 6, 88))


def on_path(series: PreparedSeries, path: int) -> MarketPath:
    return MarketPath((series,), path, rollout_count=2)


@pytest.mark.parametrize("amount", [0, 7, -7, MAX_COUNT, PreparedFixedAmount(amount=7)])
def test_fixed_amounts_keep_their_exact_count(inflation: PreparedSeries, amount: int | PreparedFixedAmount) -> None:
    expected = amount.amount if isinstance(amount, PreparedFixedAmount) else amount
    for path in (0, 1):
        assert on_path(inflation, path).amount(amount, 5) == expected


@pytest.mark.parametrize("sign", [1, -1])
def test_indexed_quotes_follow_resets_and_round_half_away_from_zero(inflation: PreparedSeries, sign: int) -> None:
    amount = PreparedIndexedAmount(
        base_amount=sign * 3, series_id="inflation", base_month_index=0, adjustment_period_months=2
    )
    for path, expected in enumerate(((3, 3, 8, 8, 17, 17), (3, 3, 2, 2, 5, 5))):
        market = on_path(inflation, path)
        assert [market.amount(amount, month) for month in range(6)] == [sign * value for value in expected]


def test_nonzero_base_month_and_prebase_rejection(inflation: PreparedSeries) -> None:
    amount = PreparedIndexedAmount(base_amount=3, series_id="inflation", base_month_index=2, adjustment_period_months=2)
    market = on_path(inflation, 0)
    assert [market.amount(amount, month) for month in range(2, 6)] == [3, 3, 7, 7]
    with pytest.raises(ValueError, match="precedes"):
        market.amount(amount, 1)


@pytest.mark.parametrize("month", [-1, 6])
def test_value_rejects_out_of_path_month(inflation: PreparedSeries, month: int) -> None:
    with pytest.raises(ValueError, match="no value"):
        on_path(inflation, 0).value("inflation", month)


def test_zero_base_level_is_not_silently_accepted(inflation: PreparedSeries) -> None:
    invalid = replace(inflation, values=(0, *inflation.values[1:]))
    amount = PreparedIndexedAmount(base_amount=3, series_id="inflation", base_month_index=0, adjustment_period_months=2)
    with pytest.raises(ZeroDivisionError):
        on_path(invalid, 0).amount(amount, 2)


def test_indexed_result_range_is_checked_without_losing_valid_large_counts(inflation: PreparedSeries) -> None:
    amount = PreparedIndexedAmount(
        base_amount=MAX_COUNT, series_id="inflation", base_month_index=0, adjustment_period_months=2
    )
    market = on_path(inflation, 0)
    assert market.amount(amount, 0) == MAX_COUNT
    with pytest.raises(OverflowError, match="overflow"):
        market.amount(amount, 2)


if __name__ == "__main__":
    pytest_bazel.main()
