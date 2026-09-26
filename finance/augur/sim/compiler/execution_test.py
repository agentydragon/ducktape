"""Checks on the sampled paths a composed world reads."""

from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.exogenous import LevelFrames
from finance.augur.model.series import SecurityDistributionKey
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.testing.security_distributions import FUND, HORIZON, PER_UNIT, PRICE, SYMBOL


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
    compile_series(paths, rollout_count=1, horizon_months=HORIZON, currency_quantum=Decimal("0.01"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_distribution_is_not_treated_as_a_zero_payout(value: float) -> None:
    with pytest.raises(ValueError, match=r"security_distribution:bnd.*no finite level at rollout 0, month 6"):
        _compile(_payout_paths(bad_month_value=value))


def test_missing_distribution_snapshot_is_not_treated_as_a_zero_payout() -> None:
    kind = SecurityDistributionKey(symbol=SYMBOL).kind
    frames = dict(_payout_paths().levels.by_kind)
    frames[kind] = frames[kind].filter(pl.col("month_index") != 6)
    with pytest.raises(ValueError, match=r"security_distribution:bnd.*no finite level at rollout 0, month 6"):
        _compile(ExternalSeriesContext(levels=LevelFrames.from_partial(frames)))


@pytest.mark.parametrize("value", [-0.1, -1e-15])
def test_negative_distribution_is_rejected_even_if_it_would_round_to_zero(value: float) -> None:
    with pytest.raises(ValueError, match=r"security_distribution:bnd.*negative payout at rollout 0, month 6"):
        _compile(_payout_paths(bad_month_value=value))


if __name__ == "__main__":
    pytest_bazel.main()
