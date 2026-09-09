"""Compose different products on the same loaded/sampled markets without resampling."""

from dataclasses import replace
from datetime import date
from unittest.mock import patch

import numpy as np
import pytest
import pytest_bazel
from polars.testing import assert_frame_equal

from finance.augur.model.bond_fund import BondFundSpec, YieldCurve
from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.historical_windows import HistoricalWindowsModel, MacroHistory
from finance.augur.model.market_paths import MarketPaths
from finance.augur.model.product_paths import construct_products
from finance.augur.model.series import InflationKey, SecurityDistributionKey, SecurityKey
from finance.augur.model.structural_macro import (
    EquityProcess,
    MacroVarSpec,
    StructuralMacroModel,
    StructuralMacroProviderConfig,
)


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


@pytest.fixture
def structural() -> StructuralMacroModel:
    return StructuralMacroProviderConfig(
        macro_state=MacroVarSpec(
            initial_state=(-0.01, 0.03, 0.025),
            intercept=(0.003, 0.003, 0.0025),
            transition=((0.9, 0.0, 0.0), (0.0, 0.9, 0.0), (0.0, 0.0, 0.9)),
            shock_cholesky=((0.001, 0.0, 0.0), (0.0, 0.001, 0.0), (0.0, 0.0, 0.001)),
        ),
        initial_inflation_level=137.0,
        equity=EquityProcess(
            instrument=EquitySpec(symbol="test_equity", initial_price_usd=517.3),
            monthly_log_return_mu=0.003,
            monthly_log_return_sigma=0.01,
            rate_beta=-0.7,
        ),
    ).realize_model()


def _check_two_constructions(paths: MarketPaths) -> None:
    assert paths.equity_total_return_index is not None
    inputs = [
        paths.short_rate,
        paths.term_spread,
        paths.cpi_level,
        paths.equity_total_return_index,
        *paths.corporate_yields.values(),
    ]
    before = [array.copy() for array in inputs]
    equity = EquitySpec(symbol="test_equity", initial_price_usd=517.3)
    short = BondFundSpec(symbol="test_fund", maturity_years=2.0, initial_price_usd=98.7)
    long = BondFundSpec(symbol="test_fund", maturity_years=8.0, initial_price_usd=98.7)
    with (
        patch.object(HistoricalWindowsModel, "market_paths", side_effect=AssertionError("must not reload markets")),
        patch.object(StructuralMacroModel, "sample_market", side_effect=AssertionError("must not resample markets")),
    ):
        first = construct_products(paths, equity=equity, instruments=(short,))
        second = construct_products(paths, equity=equity, instruments=(long,))
        repeated = construct_products(paths, equity=equity, instruments=(short,))
    for key in first.levels.series_keys():
        assert_frame_equal(first.levels.frame(key.kind), repeated.levels.frame(key.kind))
    for key in (SecurityKey(symbol=equity.symbol), InflationKey()):
        np.testing.assert_array_equal(
            first.level_matrix(key, rollout_count=paths.rollout_count, horizon_months=paths.horizon_months),
            second.level_matrix(key, rollout_count=paths.rollout_count, horizon_months=paths.horizon_months),
        )
    key = SecurityKey(symbol=short.symbol)
    assert not np.array_equal(
        first.level_matrix(key, rollout_count=paths.rollout_count, horizon_months=paths.horizon_months),
        second.level_matrix(key, rollout_count=paths.rollout_count, horizon_months=paths.horizon_months),
    )
    assert SecurityDistributionKey(symbol=equity.symbol) not in first.levels.series_keys()
    for actual, expected in zip(inputs, before, strict=True):
        np.testing.assert_array_equal(actual, expected)
    for name, value in paths.provenance.items():
        assert first.provenance[name] == value


def test_historical_markets_are_reusable_and_keep_observed_credit(historical: HistoricalWindowsModel) -> None:
    dates = (date(2000, 4, 1), date(2000, 1, 1))
    paths = historical.market_paths(window_starts=dates, horizon_months=12)
    _check_two_constructions(paths)
    assert paths.provenance["window_starts"] == tuple(month.isoformat() for month in dates)
    assert paths.short_rate[1, 0] == -0.01
    for curve, expected in ((YieldCurve.CORPORATE_AAA, 0.063), (YieldCurve.CORPORATE_BAA, 0.086)):
        fund = BondFundSpec(symbol="test_credit", maturity_years=5, yield_curve=curve)
        bundle = construct_products(paths, equity=None, instruments=(fund,))
        payout = bundle.level_matrix(SecurityDistributionKey(symbol=fund.symbol), rollout_count=2, horizon_months=12)
        assert payout[0, 0] == pytest.approx(100.0 * expected / 12)


def test_structural_markets_are_reusable_but_do_not_invent_credit(structural: StructuralMacroModel) -> None:
    request = ExogenousSamplingRequest(horizon_months=12, rollout_seeds=(71, 12))
    paths = structural.sample_market(request)
    _check_two_constructions(paths)
    assert paths.provenance["rollout_seeds"] == (71, 12)
    assert paths.short_rate[0, 0] == -0.01
    with pytest.raises(ValueError, match=r"cannot produce.*no credit factor"):
        construct_products(
            paths,
            equity=None,
            instruments=(BondFundSpec(symbol="test_credit", maturity_years=5, yield_curve=YieldCurve.CORPORATE_AAA),),
        )


def test_equity_requires_an_actual_equity_market_path(structural: StructuralMacroModel) -> None:
    paths = replace(
        structural.sample_market(ExogenousSamplingRequest(horizon_months=1, rollout_seeds=(1,))),
        equity_total_return_index=None,
    )
    with pytest.raises(ValueError, match="no equity total-return path"):
        construct_products(paths, equity=EquitySpec(symbol="test_equity", initial_price_usd=100.0), instruments=())


if __name__ == "__main__":
    pytest_bazel.main()
