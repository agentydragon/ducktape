"""Tests for the independent-rate Ornstein-Uhlenbeck fit.

Recovery from SYNTHETIC paths with known parameters, which is the only way to test an
estimator: a test against real FRED data could only assert that today's numbers equal
today's numbers, and would break on every evidence refresh while catching nothing.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest
import pytest_bazel

from finance.augur.fit.ornstein_uhlenbeck import OrnsteinUhlenbeckFit, fit_ornstein_uhlenbeck, fit_rates_block
from finance.augur.fit.synthetic_rates import TRUE_MEAN, TRUE_REVERSION, TRUE_SIGMA, months, ou_path
from finance.augur.model.structural_macro import MINIMUM_MONTHS, PERCENT_TO_DECIMAL
from finance.evidence.loading import MonthlyLevel


def test_recovers_the_parameters_it_was_generated_from() -> None:
    """The estimator works. Long sample, so the finite-sample bias the module documents is
    small enough not to be what this test is measuring."""

    fit = fit_ornstein_uhlenbeck(ou_path(12_000))

    assert fit.reversion_per_month == pytest.approx(TRUE_REVERSION, rel=0.15)
    assert fit.long_run_mean == pytest.approx(TRUE_MEAN, rel=0.05)
    assert fit.monthly_sigma == pytest.approx(TRUE_SIGMA, rel=0.05)


def test_reversion_is_biased_upward_at_a_realistic_sample_length() -> None:
    """The caveat the module states, as a measurement rather than a citation.

    865 months is the real `FEDFUNDS`/`GS10` overlap. At that length OLS pulls the AR(1)
    coefficient toward zero, so the fitted pull is systematically FASTER than the truth — which
    means a fitted half-life should be read as an upper bound on the speed, and a run whose
    answer turns on the rate settling quickly is trusting the bias rather than the data.
    """

    fits = [fit_ornstein_uhlenbeck(ou_path(865, seed=seed)) for seed in range(24)]
    median_reversion = float(np.median([fit.reversion_per_month for fit in fits]))

    assert median_reversion > TRUE_REVERSION
    # Sizeable — the same order as the bias formula (1 + 3b)/n predicts — but not unbounded.
    assert median_reversion < TRUE_REVERSION * 1.6
    # Sigma, by contrast, is clean at this length. That asymmetry is why the module says to
    # read sigma and treat the mean and the speed as soft.
    assert float(np.median([fit.monthly_sigma for fit in fits])) == pytest.approx(TRUE_SIGMA, rel=0.05)


def test_the_starting_level_is_todays_observation_not_the_fitted_mean() -> None:
    """A rate path starts where the rate actually IS. Anchoring it at the long-run mean instead
    would erase the single most decision-relevant fact about the present."""

    path = ou_path(600, initial=0.15)
    fit = fit_ornstein_uhlenbeck(path)

    assert fit.latest_level == path[-1].value
    assert fit.latest_month == path[-1].month
    assert fit.sample_months == 600


def test_half_life_follows_from_the_reversion() -> None:
    """A pure function of the fitted reversion, so it is constructed rather than estimated —
    routing it through a fit would test the estimator again and call it a half-life test."""

    fit = OrnsteinUhlenbeckFit(
        reversion_per_month=1.0 - 0.5 ** (1 / 60),
        long_run_mean=0.03,
        monthly_sigma=0.002,
        latest_level=0.04,
        latest_month=date(2026, 7, 1),
        sample_months=600,
    )

    assert fit.half_life_years == pytest.approx(5.0)


def test_a_non_reverting_series_is_rejected_rather_than_given_a_long_run_mean() -> None:
    """`mean = a / (1 - b)` is undefined at b = 1 and sign-flips above it, so a fit that got
    there would hand the sampler a process drifting without bound over 30 years.

    A deterministic ramp, not a random walk, and the difference is the point: a SAMPLED random
    walk fits as slowly mean-reverting, because the same finite-sample bias that inflates
    reversion pulls `b` below one. So this guard catches the degenerate case and cannot catch
    a unit root in real data — `test_reversion_is_biased_upward_at_a_realistic_sample_length`
    is what documents that, and reading the fitted mean as a forecast is what it costs.
    """

    ramp = [MonthlyLevel(month=month, value=float(index)) for index, month in enumerate(months(600), start=1)]
    with pytest.raises(ValueError, match="not mean-reverting"):
        fit_ornstein_uhlenbeck(ramp)


def test_too_few_observations_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 240"):
        fit_ornstein_uhlenbeck(ou_path(MINIMUM_MONTHS - 1))


def test_unsorted_input_is_rejected() -> None:
    """An AR(1) is entirely a claim about consecutive pairs, so shuffled input does not fail —
    it fits something meaningless. Cheap to reject, impossible to notice downstream."""

    path = ou_path(600)
    with pytest.raises(ValueError, match="sorted oldest first"):
        fit_ornstein_uhlenbeck([path[1], path[0], *path[2:]])


def test_the_spread_is_fitted_on_the_difference_not_on_the_two_levels() -> None:
    """The sharp version of the claim: two rates that wander a lot with a CONSTANT gap have a
    spread with essentially no volatility. Fitting `GS10` on its own and subtracting would
    report the long rate's volatility as the spread's, and the spread is what prices duration.
    """

    short = ou_path(900, sigma=0.006, seed=3)
    gap = 0.011
    fit = fit_rates_block(
        short_rate_percent=[MonthlyLevel(month=m.month, value=m.value / PERCENT_TO_DECIMAL) for m in short],
        long_rate_percent=[MonthlyLevel(month=m.month, value=(m.value + gap) / PERCENT_TO_DECIMAL) for m in short],
        window_start=date(1900, 1, 1),
    )

    assert fit.term_spread.monthly_sigma == pytest.approx(0.0, abs=1e-9)
    assert fit.term_spread.latest_level == pytest.approx(gap)
    assert fit.short_rate.monthly_sigma == pytest.approx(0.006, rel=0.1)


def test_percent_input_becomes_decimal() -> None:
    """FRED publishes both series in percent; every rate inside augur is a decimal. A missed
    conversion is a 100x error that still produces a plausible-looking mean-reverting path."""

    short = ou_path(600, mean=0.04, sigma=0.002, seed=5)
    long_rate = ou_path(600, mean=0.05, sigma=0.002, seed=6)
    fit = fit_rates_block(
        short_rate_percent=[MonthlyLevel(month=m.month, value=m.value * 100.0) for m in short],
        long_rate_percent=[MonthlyLevel(month=m.month, value=m.value * 100.0) for m in long_rate],
        window_start=date(1900, 1, 1),
    )

    assert fit.short_rate.long_run_mean == pytest.approx(0.04, abs=0.01)
    assert fit.short_rate.latest_level == pytest.approx(short[-1].value)


def test_the_window_trims_and_reports_what_it_trimmed_to() -> None:
    short = ou_path(900)
    window_start = short[300].month
    fit = fit_rates_block(
        short_rate_percent=[MonthlyLevel(month=m.month, value=m.value * 100.0) for m in short],
        long_rate_percent=[MonthlyLevel(month=m.month, value=m.value * 100.0 + 1.0) for m in short],
        window_start=window_start,
    )

    assert fit.window_start == window_start
    assert fit.short_rate.sample_months == 600


def test_the_two_series_are_joined_on_month() -> None:
    """The series start eight months apart in reality. A positional zip would pair each month's
    short rate with a different month's long rate — a spread on the wrong month, which is
    invisible in the level and wrong in exactly the parameter duration is priced from."""

    short = ou_path(900, seed=7)
    fit = fit_rates_block(
        short_rate_percent=[MonthlyLevel(month=m.month, value=m.value * 100.0) for m in short],
        # Starts 8 months later and ends 8 months earlier, as a real pair of FRED series would.
        long_rate_percent=[MonthlyLevel(month=m.month, value=(m.value + 0.01) * 100.0) for m in short[8:-8]],
        window_start=date(1900, 1, 1),
    )

    assert fit.short_rate.sample_months == len(short) - 16
    assert fit.term_spread.monthly_sigma == pytest.approx(0.0, abs=1e-9)


if __name__ == "__main__":
    pytest_bazel.main()
