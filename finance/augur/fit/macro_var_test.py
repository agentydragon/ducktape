"""Tests for the joint macro VAR(1) fit.

Recovery from a SYNTHETIC path with known parameters, which is the only way to test an
estimator: a test against real FRED data could only assert that today's numbers equal today's
numbers, and would break on every evidence refresh while catching nothing.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import pytest_bazel

from finance.augur.fit.macro_var import fit_macro_var
from finance.augur.fit.synthetic_rates import months
from finance.augur.model.structural_macro import PERCENT_TO_DECIMAL
from finance.evidence.loading import MonthlyLevel

TRUE_MACRO_INTERCEPT = (0.00012, 0.00006, 0.00006)
TRUE_MACRO_TRANSITION = ((0.97, 0.02, 0.02), (0.0, 0.9, 0.0), (0.0, 0.0, 0.98))
TRUE_MACRO_SHOCK_CHOLESKY = ((0.003, 0.0, 0.0), (0.001, 0.002, 0.0), (0.0005, 0.0003, 0.0015))


def _macro_var_path(
    n_months: int,
    *,
    intercept: tuple[float, float, float] = TRUE_MACRO_INTERCEPT,
    transition: tuple[tuple[float, float, float], ...] = TRUE_MACRO_TRANSITION,
    shock_cholesky: tuple[tuple[float, float, float], ...] = TRUE_MACRO_SHOCK_CHOLESKY,
    seed: int = 0,
) -> tuple[list[MonthlyLevel], list[MonthlyLevel], list[MonthlyLevel]]:
    """Simulate a synthetic `(short_rate, term_spread, inflation_rate)` VAR(1) path and invert
    it into the `(short_rate_percent, long_rate_percent, cpi_level)` inputs `fit_macro_var`
    actually takes: short/long rate as FRED-style percent, and a CPI level series whose
    12-month log difference reproduces the simulated inflation_rate exactly — `fit_macro_var`
    reads inflation as trailing-year log CPI growth, not a rate observed directly."""

    rng = np.random.default_rng(seed)
    all_months = months(n_months)
    transition_matrix = np.array(transition)
    chol = np.array(shock_cholesky)

    state = np.empty((n_months, 3))
    previous = np.zeros(3)
    for t in range(n_months):
        previous = np.array(intercept) + transition_matrix @ previous + chol @ rng.standard_normal(3)
        state[t] = previous
    short_rate, spread, inflation_rate = state[:, 0], state[:, 1], state[:, 2]
    long_rate = short_rate + spread

    # cpi[t] = cpi[t-12] * exp(inflation_rate[t]); the first year never appears in a "usable"
    # state (fit_macro_var trims the first inflation_lookback_months), so its seed is arbitrary.
    cpi = np.empty(n_months)
    cpi[:12] = 100.0
    for t in range(12, n_months):
        cpi[t] = cpi[t - 12] * math.exp(inflation_rate[t])

    short_rate_percent = [
        MonthlyLevel(month=m, value=v / PERCENT_TO_DECIMAL) for m, v in zip(all_months, short_rate, strict=True)
    ]
    long_rate_percent = [
        MonthlyLevel(month=m, value=v / PERCENT_TO_DECIMAL) for m, v in zip(all_months, long_rate, strict=True)
    ]
    cpi_level = [MonthlyLevel(month=m, value=v) for m, v in zip(all_months, cpi, strict=True)]
    return short_rate_percent, long_rate_percent, cpi_level


def test_macro_var_recovers_the_parameters_it_was_generated_from() -> None:
    """The joint estimator works, on a synthetic path with known intercept/transition/
    shock_cholesky — the only way to test it, per the module docstring. A long sample (1000
    years of synthetic months) puts OLS well past the finite-sample bias
    `ornstein_uhlenbeck_test.test_reversion_is_biased_upward_at_a_realistic_sample_length`
    documents for the single-rate estimator; `transition`'s true off-diagonal zeros are why
    these use `abs`, not `rel`.
    """

    n_months = 12_000
    short_rate_percent, long_rate_percent, cpi_level = _macro_var_path(n_months)
    fit = fit_macro_var(short_rate_percent=short_rate_percent, long_rate_percent=long_rate_percent, cpi_level=cpi_level)

    assert fit.intercept == pytest.approx(TRUE_MACRO_INTERCEPT, abs=1e-4)
    assert np.array(fit.transition) == pytest.approx(np.array(TRUE_MACRO_TRANSITION), abs=0.03)
    assert np.array(fit.shock_cholesky) == pytest.approx(np.array(TRUE_MACRO_SHOCK_CHOLESKY), abs=1e-4)

    all_months = months(n_months)
    assert fit.first_month == all_months[12]
    assert fit.latest_month == all_months[-1]
    assert fit.sample_months == n_months - 12


if __name__ == "__main__":
    pytest_bazel.main()
