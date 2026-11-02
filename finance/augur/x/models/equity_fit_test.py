"""Tests for the equity log-return and rate-beta fits.

Recovery from SYNTHETIC paths with known parameters, which is the only way to test an
estimator: a test against real FRED/market data could only assert that today's numbers equal
today's numbers, and would break on every evidence refresh while catching nothing.
"""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np
import pytest
import pytest_bazel

from finance.augur.x.models.equity_fit import RateBetaFit, fit_log_returns, fit_rate_beta
from finance.augur.x.models.synthetic_rates import months, ou_path
from finance.evidence.loading import MonthlyLevel


def _geometric_path(
    count: int, *, mu: float, sigma: float, seed: int = 0, initial: float = 100.0
) -> list[MonthlyLevel]:
    rng = np.random.default_rng(seed)
    levels = initial * np.exp(np.cumsum(np.concatenate([[0.0], mu + sigma * rng.standard_normal(count - 1)])))
    return [MonthlyLevel(month=m, value=float(v)) for m, v in zip(months(count), levels, strict=True)]


def _fit_from_monthly_levels(*, equity_levels: list[MonthlyLevel], short_rate: list[MonthlyLevel]) -> RateBetaFit:
    equity = {level.month: level.value for level in equity_levels}
    rates = {level.month: level.value for level in short_rate}
    shared = sorted(equity.keys() & rates.keys())
    return fit_rate_beta(
        months=shared,
        equity_levels=np.array([equity[month] for month in shared]),
        short_rate=np.array([rates[month] for month in shared]),
    )


def test_log_return_fit_recovers_drift_and_volatility() -> None:
    fit = fit_log_returns(_geometric_path(6_000, mu=0.008, sigma=0.043))

    assert fit.monthly_log_mu == pytest.approx(0.008, rel=0.1)
    assert fit.monthly_log_sigma == pytest.approx(0.043, rel=0.05)


def test_log_returns_need_positive_levels() -> None:
    """A level series is multiplicative; a zero or negative level has no log return and would
    otherwise propagate as a silent nan through the drift."""

    path = _geometric_path(300, mu=0.005, sigma=0.02)
    with pytest.raises(ValueError, match="strictly positive"):
        fit_log_returns([*path[:-1], MonthlyLevel(month=path[-1].month, value=0.0)])


def test_rate_beta_recovers_a_planted_coupling() -> None:
    """The estimator finds a coupling that is really there, and reports that it explains most
    of the variance when it does."""

    rates = ou_path(900, reversion=0.05, mean=0.04, sigma=0.004, seed=11)
    beta = -2.5
    equity_levels, level = [], 100.0
    for previous, current in pairwise(rates):
        level *= float(np.exp(0.006 + beta * (current.value - previous.value)))
        equity_levels.append(MonthlyLevel(month=current.month, value=level))

    fit = _fit_from_monthly_levels(equity_levels=equity_levels, short_rate=rates)

    assert fit.beta == pytest.approx(beta, rel=0.05)
    assert fit.r_squared > 0.95


def test_rate_beta_reports_near_zero_explanatory_power_when_there_is_no_coupling() -> None:
    """The case the real data is in, and the reason `r_squared` is a field rather than a note:
    an uncoupled equity series still produces some nonzero beta, and only R² says to ignore it.
    """

    rates = ou_path(900, reversion=0.05, mean=0.04, sigma=0.004, seed=13)
    equity = _geometric_path(900, mu=0.007, sigma=0.043, seed=17)
    fit = _fit_from_monthly_levels(
        equity_levels=[MonthlyLevel(month=r.month, value=e.value) for r, e in zip(rates, equity, strict=True)],
        short_rate=rates,
    )

    assert fit.r_squared < 0.02
    assert fit.sample_months == 900


def test_rate_beta_needs_enough_shared_months() -> None:
    rates = ou_path(300, seed=19)
    with pytest.raises(ValueError, match="shared months"):
        _fit_from_monthly_levels(
            equity_levels=[MonthlyLevel(month=r.month, value=100.0 + i) for i, r in enumerate(rates[:100])],
            short_rate=rates,
        )


def test_rate_beta_skips_the_transition_across_a_missing_month() -> None:
    """The return and rate change across a source gap are multi-month observations, not one
    monthly beta sample. Consecutive months on each side remain usable.
    """

    all_months = months(500)
    rng = np.random.default_rng(27)
    rate_values = np.cumsum(rng.normal(0.0, 0.002, len(all_months))) + 0.04
    beta = -1.7
    monthly_returns = np.zeros(len(all_months))
    monthly_returns[1:] = 0.003 + beta * np.diff(rate_values) + rng.normal(0.0, 0.02, len(all_months) - 1)
    equity_values = 100.0 * np.exp(np.cumsum(monthly_returns))
    missing_index = 350
    retained_indices = [index for index in range(len(all_months)) if index != missing_index]
    rates = [MonthlyLevel(month=all_months[index], value=float(rate_values[index])) for index in retained_indices]
    equity = [MonthlyLevel(month=all_months[index], value=float(equity_values[index])) for index in retained_indices]

    fit = _fit_from_monthly_levels(equity_levels=equity, short_rate=rates)

    consecutive = [(left, right) for left, right in pairwise(retained_indices) if right == left + 1]
    expected_returns = np.array([math.log(equity_values[right] / equity_values[left]) for left, right in consecutive])
    expected_rate_changes = np.array([rate_values[right] - rate_values[left] for left, right in consecutive])
    expected_slope, _ = np.polyfit(expected_rate_changes, expected_returns, 1)

    assert fit.beta == pytest.approx(expected_slope)
    assert fit.sample_months == len(retained_indices)


def test_rate_beta_rejects_invalid_aligned_inputs() -> None:
    axis = months(300)
    values = np.ones(300)
    with pytest.raises(ValueError, match="match the monthly axis"):
        fit_rate_beta(months=axis, equity_levels=values[:-1], short_rate=values)
    with pytest.raises(ValueError, match="unique, increasing"):
        fit_rate_beta(months=[*axis[:-1], axis[-2]], equity_levels=values, short_rate=values)
    with pytest.raises(ValueError, match="positive and finite"):
        fit_rate_beta(months=axis, equity_levels=np.zeros(300), short_rate=values)


if __name__ == "__main__":
    pytest_bazel.main()
