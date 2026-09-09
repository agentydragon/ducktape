"""Validate the alignment and index convention required by direct construction callers."""

from dataclasses import replace

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.bond_fund import YieldCurve
from finance.augur.model.market_paths import MarketPaths


@pytest.fixture
def paths() -> MarketPaths:
    return MarketPaths(
        short_rate=np.full((2, 4), 0.04),
        term_spread=np.full((2, 4), 0.01),
        cpi_level=np.full((2, 4), 100.0),
        equity_total_return_index=np.ones((2, 4)),
        corporate_yields={YieldCurve.CORPORATE_AAA: np.full((2, 4), 0.06)},
        model_id="test_market",
        provenance={},
    )


@pytest.mark.parametrize("shape", [(4,), (2, 0), (0, 4), (2, 4, 1)])
def test_paths_need_rollout_and_opening_month_axes(paths: MarketPaths, shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="opening observation"):
        replace(paths, short_rate=np.zeros(shape))


def test_every_optional_and_required_array_must_align(paths: MarketPaths) -> None:
    wrong = np.ones((1, 4))
    with pytest.raises(ValueError, match="share their rollout/month axes"):
        replace(paths, term_spread=wrong)
    with pytest.raises(ValueError, match="share their rollout/month axes"):
        replace(paths, cpi_level=wrong)
    with pytest.raises(ValueError, match="share their rollout/month axes"):
        replace(paths, equity_total_return_index=wrong)
    with pytest.raises(ValueError, match="share their rollout/month axes"):
        replace(paths, corporate_yields={YieldCurve.CORPORATE_AAA: wrong})


def test_a_price_level_cannot_masquerade_as_a_unit_total_return_index(paths: MarketPaths) -> None:
    with pytest.raises(ValueError, match="start at one"):
        replace(paths, equity_total_return_index=np.full((2, 4), 100.0))


def test_corporate_inputs_cannot_silently_override_treasury_state(paths: MarketPaths) -> None:
    with pytest.raises(ValueError, match="must not override"):
        replace(paths, corporate_yields={YieldCurve.GOVERNMENT: np.full((2, 4), 0.06)})


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_markets_are_rejected(paths: MarketPaths, invalid: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        replace(paths, short_rate=np.full((2, 4), invalid))
    with pytest.raises(ValueError, match="finite"):
        replace(paths, corporate_yields={YieldCurve.CORPORATE_AAA: np.full((2, 4), invalid)})
    with pytest.raises(ValueError, match="finite"):
        replace(paths, cpi_level=np.full((2, 4), invalid))
    with pytest.raises(ValueError, match="finite"):
        replace(paths, equity_total_return_index=np.full((2, 4), invalid))


@pytest.mark.parametrize("invalid", [0.0, -1.0])
def test_cpi_and_equity_levels_must_stay_positive(paths: MarketPaths, invalid: float) -> None:
    with pytest.raises(ValueError, match="CPI levels must be strictly positive"):
        replace(paths, cpi_level=np.full((2, 4), invalid))
    equity = np.ones((2, 4))
    equity[:, 1] = invalid
    with pytest.raises(ValueError, match="equity total-return indices must be strictly positive"):
        replace(paths, equity_total_return_index=equity)


def test_negative_market_rates_are_valid(paths: MarketPaths) -> None:
    negative = replace(paths, short_rate=np.full((2, 4), -0.01), term_spread=np.full((2, 4), -0.02))
    assert negative.short_rate[0, 0] == -0.01


if __name__ == "__main__":
    pytest_bazel.main()
