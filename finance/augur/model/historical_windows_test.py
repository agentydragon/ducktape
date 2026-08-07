"""Tests for the historical-window sampler.

Synthetic histories with a KNOWN shape, so an assertion names a property of the replay rather
than a fact about FRED. A test against the real record would assert that the past equals the
past, and would break on every evidence refresh while catching nothing.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.historical_windows import HistoricalWindowsModel, MacroHistory, macro_history_from_levels
from finance.augur.model.series import InflationKey, SecurityDistributionKey, SecurityKey, SecuritySymbol
from finance.augur.model.structural_macro import EquitySpec, InstrumentSpec

BOND = SecuritySymbol("BOND")
CASH = SecuritySymbol("CASH")
EQUITY = SecuritySymbol("EQ")
MONTHS = 600


def _history(months: int = MONTHS) -> MacroHistory:
    """A record whose every series is strictly increasing, so a window's identity is visible in
    its values: window `i` starts at exactly the month-`i` level of each series."""

    index = np.arange(months, dtype=np.float64)
    # Equity and CPI grow at an ACCELERATING rate, not a constant one. A constant-growth series
    # is shape-invariant under rebasing, so every window would replay identically and the tests
    # that distinguish windows would pass against a sampler that always returned window zero.
    return MacroHistory(
        short_rate=0.01 + index * 0.0001,
        term_spread=0.005 + index * 0.00001,
        equity_level=100.0 * np.exp(np.cumsum(0.004 + index * 0.00001)),
        cpi_level=100.0 * np.exp(np.cumsum(0.0015 + index * 0.000003)),
    )


def _model(history: MacroHistory | None = None) -> HistoricalWindowsModel:
    return HistoricalWindowsModel(
        history=history if history is not None else _history(),
        instruments=(
            InstrumentSpec(symbol=BOND, duration_years=6.0, initial_price_usd=100.0),
            InstrumentSpec(symbol=CASH, duration_years=0.0, initial_price_usd=1.0),
        ),
        equity=EquitySpec(symbol=EQUITY, initial_price_usd=500.0),
    )


def _series(model: HistoricalWindowsModel, key: object, *, horizon: int, rollouts: int) -> np.ndarray:
    bundle = model.sample(ExogenousSamplingRequest(horizon_months=horizon, rollout_seeds=tuple(range(rollouts))))
    return bundle.level_matrix(key, rollout_count=rollouts, horizon_months=horizon)  # type: ignore[arg-type]


def test_each_rollout_replays_a_different_window() -> None:
    """The whole idea: rollout `i` is a different slice of the record, not a different draw."""

    equity = _series(_model(), SecurityKey(symbol=EQUITY), horizon=120, rollouts=4)

    # Rebased to a common start, so what distinguishes windows is their SHAPE.
    assert np.all(equity[:, 0] == 500.0)
    assert len({round(float(row[-1]), 6) for row in equity}) == 4


def test_every_window_starts_at_the_configured_level() -> None:
    """A portfolio's month-0 value must not depend on which piece of history follows it."""

    model = _model()
    equity = _series(model, SecurityKey(symbol=EQUITY), horizon=120, rollouts=6)
    inflation = _series(model, InflationKey(), horizon=120, rollouts=6)

    assert np.all(equity[:, 0] == 500.0)
    assert np.all(inflation[:, 0] == 100.0)


def test_seeds_are_ignored_because_nothing_is_random() -> None:
    """A rollout's identity is its start month. Two requests with different seeds and the same
    count must replay the same windows, or the provider would be pretending to sample."""

    model = _model()
    horizon, rollouts = 120, 5
    first = model.sample(ExogenousSamplingRequest(horizon_months=horizon, rollout_seeds=tuple(range(rollouts))))
    second = model.sample(
        ExogenousSamplingRequest(horizon_months=horizon, rollout_seeds=tuple(range(1000, 1000 + rollouts)))
    )
    key = SecurityKey(symbol=EQUITY)

    assert np.array_equal(
        first.level_matrix(key, rollout_count=rollouts, horizon_months=horizon),
        second.level_matrix(key, rollout_count=rollouts, horizon_months=horizon),
    )


def test_asking_for_more_rollouts_than_windows_is_rejected() -> None:
    """The failure this guards against is silent and severe: cycling would duplicate paths and
    double-count them in every percentile, producing a confident distribution over a handful of
    windows repeated. Better to refuse and make the caller see how little data there is."""

    model = _model(_history(months=200))
    with pytest.raises(ValueError, match="supplies only"):
        model.sample(ExogenousSamplingRequest(horizon_months=120, rollout_seeds=tuple(range(500))))


def test_a_horizon_longer_than_the_record_is_rejected() -> None:
    model = _model(_history(months=100))
    with pytest.raises(ValueError, match="too few for a"):
        model.sample(ExogenousSamplingRequest(horizon_months=120, rollout_seeds=(0,)))


def test_the_independent_window_estimate_is_reported_and_is_tiny() -> None:
    """The number that bounds every conclusion drawn from this provider, and the reason it is a
    method rather than a comment: 46 years of monthly data admit ~199 overlapping 30-year
    windows and about 1.5 independent ones."""

    model = _model(_history(months=559))

    assert model.window_count(360) == 199
    assert model.independent_window_estimate(360) == pytest.approx(1.55, abs=0.01)


def test_windows_are_spread_across_the_record_rather_than_taken_from_its_start() -> None:
    """A caller asking for fewer rollouts than windows wants the record thinned, not its first
    decade. Taking a prefix would sample one era and call it history."""

    model = _model(_history(months=600))
    sparse = _series(model, SecurityKey(symbol=CASH), horizon=120, rollouts=3)
    dense = _series(model, SecurityKey(symbol=CASH), horizon=120, rollouts=480)

    # Cash's payout tracks the short rate, which rises monotonically through this record, so
    # the last thinned window must reach as high as the last dense one.
    assert float(sparse[-1, 0]) == pytest.approx(float(dense[-1, 0]), rel=1e-6)


def test_the_bond_instrument_layer_matches_the_structural_provider() -> None:
    """Shared on purpose. How a fund responds to a yield change is a claim about the fund, not
    about the economy, so a rate path fed to either provider must price it identically — that
    is what makes the two comparable at all."""

    history = _history()
    model = _model(history)
    horizon, rollouts = 120, 3
    price = _series(model, SecurityKey(symbol=BOND), horizon=horizon, rollouts=rollouts)
    payout = _series(model, SecurityDistributionKey(symbol=BOND), horizon=horizon, rollouts=rollouts)

    # The synthetic record's yields rise monotonically, so this is the 2022 shape again.
    assert np.all(price[:, -1] < price[:, 0])
    assert np.all(payout[:, -1] > payout[:, 0])


def test_the_aligned_record_is_the_intersection_of_all_four_series() -> None:
    """A total-return equity series is the short one, so it sets where usable history starts.
    Aligning on anything less than all four would put a rate from one month beside an equity
    level from another — invisible in the levels, wrong in every correlation."""

    history = macro_history_from_levels(
        short_rate_percent=[(date(2000, 1, 1) + timedelta(days=31 * m), 4.0) for m in range(100)],
        long_rate_percent=[(date(2000, 1, 1) + timedelta(days=31 * m), 5.0) for m in range(100)],
        equity_level=[(date(2000, 1, 1) + timedelta(days=31 * m), 100.0 + m) for m in range(40, 100)],
        cpi_level=[(date(2000, 1, 1) + timedelta(days=31 * m), 100.0 + m) for m in range(20, 90)],
    )

    assert history.months == 50  # months 40..89
    assert history.short_rate[0] == pytest.approx(0.04)
    assert history.term_spread[0] == pytest.approx(0.01)


def test_disjoint_series_are_rejected() -> None:
    with pytest.raises(ValueError, match="share no months"):
        macro_history_from_levels(
            short_rate_percent=[(date(2000, 1, 1) + timedelta(days=31 * m), 4.0) for m in range(10)],
            long_rate_percent=[(date(2000, 1, 1) + timedelta(days=31 * m), 5.0) for m in range(10)],
            equity_level=[(date(2000, 1, 1) + timedelta(days=31 * m), 100.0) for m in range(50, 60)],
            cpi_level=[(date(2000, 1, 1) + timedelta(days=31 * m), 100.0) for m in range(10)],
        )


def test_mismatched_history_lengths_are_rejected() -> None:
    with pytest.raises(ValueError, match="different lengths"):
        MacroHistory(short_rate=np.zeros(10), term_spread=np.zeros(10), equity_level=np.ones(9), cpi_level=np.ones(10))


def test_the_provenance_says_the_rollouts_are_not_independent() -> None:
    """A percentile over overlapping windows is a count of starting months, not a probability.
    Carried on the bundle so a consumer that logs provenance cannot lose the caveat."""

    model = _model()
    bundle = model.sample(ExogenousSamplingRequest(horizon_months=120, rollout_seeds=tuple(range(4))))

    assert bundle.provenance["distinct_windows_available"] == 480
    assert "not independent draws" in str(bundle.provenance["notes"]).lower()


if __name__ == "__main__":
    pytest_bazel.main()
