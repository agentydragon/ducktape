"""What declarations demand of the exogenous model, and checks on the sampled paths a composed world reads."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.exogenous import LevelFrames
from finance.augur.model.series import InflationKey, LevelSeriesKey, SecurityDistributionKey
from finance.augur.sim.external_series import ExternalSeriesContext, compile_series, level_series_demand
from finance.augur.sim.money import USD
from finance.augur.sim.observations import FixedCoupon, IndexedCoupon
from finance.augur.sim.testing.security_distributions import FUND, HORIZON, PER_UNIT, PRICE, SYMBOL


def demand(*, bond_coupons: tuple[FixedCoupon | IndexedCoupon, ...]) -> tuple[LevelSeriesKey, ...]:
    return level_series_demand(
        held_assets=(), bond_coupons=bond_coupons, distributing_assets=(), amounts=(), tender_policies=(), purchases=()
    )


# One dated 4% semiannual coupon on a $100 bond, with nothing priced beside it.
INDEXED = IndexedCoupon(annual_rate_ppb=40_000_000)
NOMINAL = FixedCoupon(amount=200)


def test_an_indexed_bond_demands_an_inflation_path() -> None:
    """The demand a TIPS makes that nothing else beside it need make.

    Every other level-series demand comes from something PRICED — a lot, a sleeve, a home. A
    bond has no price series at all, so an indexed one is the only instrument whose exogenous
    demand is invisible from the thing that carries it. Without it, the engine rejects a
    missing inflation path for any caller that derives its sampling request from the
    declarations, which is what the product surface does, unless it happens to want CPI anyway.

    Asserted on the demand function rather than through a run: a run supplies its own bundle
    and would pass either way, which is how the gap stayed invisible from `sim/`.
    """

    assert InflationKey() in demand(bond_coupons=(INDEXED,))
    # And not otherwise: a nominal bond's cashflows are fixed by its terms, so demanding a
    # series it never reads would fail an unmodeled-inflation deployment for no reason.
    assert InflationKey() not in demand(bond_coupons=(NOMINAL,))


def _payout_paths(*, bad_month_value: float | None = None) -> ExternalSeriesContext:
    """The fund's flat price and per-unit payout over the horizon, one month's payout replaced when given."""
    payout = np.full((1, HORIZON + 1), float(PER_UNIT))
    if bad_month_value is not None:
        payout[0, 6] = bad_month_value
    return ExternalSeriesContext.from_level_blocks(
        [(FUND, np.full((1, HORIZON + 1), float(PRICE))), (SecurityDistributionKey(symbol=SYMBOL), payout)],
        rollout_count=1,
        horizon_months=HORIZON,
    )


def _compile(paths: ExternalSeriesContext) -> None:
    compile_series(paths, rollout_count=1, horizon_months=HORIZON, currency=USD)


def test_a_complete_nonnegative_payout_path_compiles() -> None:
    _compile(_payout_paths())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_distribution_is_not_treated_as_a_zero_payout(value: float) -> None:
    with pytest.raises(ValueError, match="security_distribution:bnd"):
        _compile(_payout_paths(bad_month_value=value))


def test_missing_distribution_snapshot_is_not_treated_as_a_zero_payout() -> None:
    kind = SecurityDistributionKey(symbol=SYMBOL).kind
    frames = dict(_payout_paths().levels.by_kind)
    frames[kind] = frames[kind].filter(pl.col("month_index") != 6)
    with pytest.raises(ValueError, match="security_distribution:bnd"):
        _compile(ExternalSeriesContext(levels=LevelFrames.from_partial(frames)))


@pytest.mark.parametrize("value", [-0.1, -1e-15])
def test_negative_distribution_is_rejected_even_if_it_would_round_to_zero(value: float) -> None:
    with pytest.raises(ValueError, match="security_distribution:bnd"):
        _compile(_payout_paths(bad_month_value=value))


if __name__ == "__main__":
    pytest_bazel.main()
