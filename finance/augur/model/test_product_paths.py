"""Compose different products on the same loaded/sampled markets without resampling."""

from datetime import date

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.bond_fund import BondFundSpec, YieldCurve
from finance.augur.model.historical_windows import HistoricalWindowsModel, MacroHistory
from finance.augur.model.product_paths import construct_products
from finance.augur.model.series import SecurityDistributionKey, SecuritySymbol
from finance.augur.model.testing import check_two_constructions


@pytest.fixture
def historical() -> HistoricalWindowsModel:
    index = np.arange(16)
    return HistoricalWindowsModel(
        history=MacroHistory(
            months=tuple(date(2000 + int(i) // 12, int(i) % 12 + 1, 1) for i in index),
            short_rate=-0.01 + index * 0.003,
            term_spread=np.full(16, 0.03),
            corporate_aaa_yield=0.06 + index * 0.001,
            corporate_baa_yield=0.08 + index * 0.002,
            equity_level=517.3 * np.exp(np.cumsum(0.002 + index * 0.001)),
            cpi_level=137.5 * np.exp(index * 0.002),
        )
    )


def test_historical_markets_are_reusable_and_keep_observed_credit(historical: HistoricalWindowsModel) -> None:
    dates = (date(2000, 4, 1), date(2000, 1, 1))
    paths = historical.market_paths(window_starts=dates, horizon_months=12)
    check_two_constructions(paths)
    assert paths.provenance["window_starts"] == tuple(month.isoformat() for month in dates)
    assert paths.short_rate[1, 0] == -0.01
    for curve, expected in ((YieldCurve.CORPORATE_AAA, 0.063), (YieldCurve.CORPORATE_BAA, 0.086)):
        fund = BondFundSpec(symbol=SecuritySymbol("test_credit"), maturity_years=5, yield_curve=curve)
        bundle = construct_products(paths, equity=None, instruments=(fund,))
        payout = bundle.level_matrix(SecurityDistributionKey(symbol=fund.symbol), rollout_count=2, horizon_months=12)
        assert payout[0, 0] == pytest.approx(100.0 * expected / 12)


if __name__ == "__main__":
    pytest_bazel.main()
