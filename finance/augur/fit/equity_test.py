"""Tests for the equity log-return and rate-beta fits.

Recovery from SYNTHETIC paths with known parameters, which is the only way to test an
estimator: a test against real FRED/market data could only assert that today's numbers equal
today's numbers, and would break on every evidence refresh while catching nothing.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest
import pytest_bazel

from finance.augur.fit.equity import fit_log_returns, fit_rate_beta
from finance.augur.fit.synthetic_rates import months, ou_path
from finance.evidence.loading import MonthlyLevel


def _geometric_path(
    count: int, *, mu: float, sigma: float, seed: int = 0, initial: float = 100.0
) -> list[MonthlyLevel]:
    rng = np.random.default_rng(seed)
    levels = initial * np.exp(np.cumsum(np.concatenate([[0.0], mu + sigma * rng.standard_normal(count - 1)])))
    return [MonthlyLevel(month=m, value=float(v)) for m, v in zip(months(count), levels, strict=True)]


def test_log_return_fit_recovers_drift_and_volatility() -> None:
    fit = fit_log_returns(_geometric_path(6_000, mu=0.008, sigma=0.043))

    assert fit.monthly_log_mu == pytest.approx(0.008, rel=0.1)
    assert fit.monthly_log_sigma == pytest.approx(0.043, rel=0.05)
    assert fit.annualized_nominal_return == pytest.approx(float(np.expm1(fit.monthly_log_mu * 12)))
    assert fit.annualized_volatility == pytest.approx(float(np.sqrt(12) * fit.monthly_log_sigma))


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

    fit = fit_rate_beta(equity_levels=equity_levels, short_rate=rates)

    assert fit.beta == pytest.approx(beta, rel=0.05)
    assert fit.r_squared > 0.95


def test_rate_beta_reports_near_zero_explanatory_power_when_there_is_no_coupling() -> None:
    """The case the real data is in, and the reason `r_squared` is a field rather than a note:
    an uncoupled equity series still produces some nonzero beta, and only R² says to ignore it.
    """

    rates = ou_path(900, reversion=0.05, mean=0.04, sigma=0.004, seed=13)
    equity = _geometric_path(900, mu=0.007, sigma=0.043, seed=17)
    fit = fit_rate_beta(
        equity_levels=[MonthlyLevel(month=r.month, value=e.value) for r, e in zip(rates, equity, strict=True)],
        short_rate=rates,
    )

    assert fit.r_squared < 0.02
    assert fit.sample_months == 900


def test_rate_beta_needs_enough_shared_months() -> None:
    rates = ou_path(300, seed=19)
    with pytest.raises(ValueError, match="shared months"):
        fit_rate_beta(
            equity_levels=[MonthlyLevel(month=r.month, value=100.0 + i) for i, r in enumerate(rates[:100])],
            short_rate=rates,
        )


if __name__ == "__main__":
    pytest_bazel.main()
