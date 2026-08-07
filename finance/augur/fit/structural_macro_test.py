"""Tests for the structural macro rates fit.

Recovery from SYNTHETIC paths with known parameters, which is the only way to test an
estimator: a test against real FRED data could only assert that today's numbers equal
today's numbers, and would break on every evidence refresh while catching nothing.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import numpy as np
import pytest
import pytest_bazel

from finance.augur.fit.structural_macro import (
    MINIMUM_MONTHS,
    PERCENT_TO_DECIMAL,
    OrnsteinUhlenbeckFit,
    fit_log_returns,
    fit_ornstein_uhlenbeck,
    fit_rate_beta,
    fit_rates_block,
    splice_at_seam,
)
from finance.evidence.loading import MonthlyLevel

TRUE_REVERSION = 0.02
TRUE_MEAN = 0.035
TRUE_SIGMA = 0.004


def _months(count: int, start_year: int = 1900) -> list[date]:
    return [date(start_year + index // 12, index % 12 + 1, 1) for index in range(count)]


def _ou_path(
    count: int,
    *,
    reversion: float = TRUE_REVERSION,
    mean: float = TRUE_MEAN,
    sigma: float = TRUE_SIGMA,
    seed: int = 0,
    initial: float | None = None,
) -> list[MonthlyLevel]:
    rng = np.random.default_rng(seed)
    value = mean if initial is None else initial
    values = []
    for shock in rng.standard_normal(count):
        value = value + reversion * (mean - value) + sigma * shock
        values.append(value)
    return [MonthlyLevel(month=month, value=v) for month, v in zip(_months(count), values, strict=True)]


def test_recovers_the_parameters_it_was_generated_from() -> None:
    """The estimator works. Long sample, so the finite-sample bias the module documents is
    small enough not to be what this test is measuring."""

    fit = fit_ornstein_uhlenbeck(_ou_path(12_000))

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

    fits = [fit_ornstein_uhlenbeck(_ou_path(865, seed=seed)) for seed in range(24)]
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

    path = _ou_path(600, initial=0.15)
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

    ramp = [MonthlyLevel(month=month, value=float(index)) for index, month in enumerate(_months(600), start=1)]
    with pytest.raises(ValueError, match="not mean-reverting"):
        fit_ornstein_uhlenbeck(ramp)


def test_too_few_observations_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 240"):
        fit_ornstein_uhlenbeck(_ou_path(MINIMUM_MONTHS - 1))


def test_unsorted_input_is_rejected() -> None:
    """An AR(1) is entirely a claim about consecutive pairs, so shuffled input does not fail —
    it fits something meaningless. Cheap to reject, impossible to notice downstream."""

    path = _ou_path(600)
    with pytest.raises(ValueError, match="sorted oldest first"):
        fit_ornstein_uhlenbeck([path[1], path[0], *path[2:]])


def test_the_spread_is_fitted_on_the_difference_not_on_the_two_levels() -> None:
    """The sharp version of the claim: two rates that wander a lot with a CONSTANT gap have a
    spread with essentially no volatility. Fitting `GS10` on its own and subtracting would
    report the long rate's volatility as the spread's, and the spread is what prices duration.
    """

    short = _ou_path(900, sigma=0.006, seed=3)
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

    short = _ou_path(600, mean=0.04, sigma=0.002, seed=5)
    long_rate = _ou_path(600, mean=0.05, sigma=0.002, seed=6)
    fit = fit_rates_block(
        short_rate_percent=[MonthlyLevel(month=m.month, value=m.value * 100.0) for m in short],
        long_rate_percent=[MonthlyLevel(month=m.month, value=m.value * 100.0) for m in long_rate],
        window_start=date(1900, 1, 1),
    )

    assert fit.short_rate.long_run_mean == pytest.approx(0.04, abs=0.01)
    assert fit.short_rate.latest_level == pytest.approx(short[-1].value)


def test_the_window_trims_and_reports_what_it_trimmed_to() -> None:
    short = _ou_path(900)
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

    short = _ou_path(900, seed=7)
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


def _geometric_path(
    count: int, *, mu: float, sigma: float, seed: int = 0, initial: float = 100.0
) -> list[MonthlyLevel]:
    rng = np.random.default_rng(seed)
    levels = initial * np.exp(np.cumsum(np.concatenate([[0.0], mu + sigma * rng.standard_normal(count - 1)])))
    return [MonthlyLevel(month=m, value=float(v)) for m, v in zip(_months(count), levels, strict=True)]


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

    rates = _ou_path(900, reversion=0.05, mean=0.04, sigma=0.004, seed=11)
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

    rates = _ou_path(900, reversion=0.05, mean=0.04, sigma=0.004, seed=13)
    equity = _geometric_path(900, mu=0.007, sigma=0.043, seed=17)
    fit = fit_rate_beta(
        equity_levels=[MonthlyLevel(month=r.month, value=e.value) for r, e in zip(rates, equity, strict=True)],
        short_rate=rates,
    )

    assert fit.r_squared < 0.02
    assert fit.sample_months == 900


def test_rate_beta_needs_enough_shared_months() -> None:
    rates = _ou_path(300, seed=19)
    with pytest.raises(ValueError, match="shared months"):
        fit_rate_beta(
            equity_levels=[MonthlyLevel(month=r.month, value=100.0 + i) for i, r in enumerate(rates[:100])],
            short_rate=rates,
        )


def _levels(start_year: int, values: list[float]) -> list[MonthlyLevel]:
    return [MonthlyLevel(month=m, value=v) for m, v in zip(_months(len(values), start_year), values, strict=True)]


def test_the_splice_shifts_the_early_series_to_meet_the_late_one() -> None:
    """The seam is where continuity matters: a 30-year window starting in 1926 crosses 1953, so
    a step there would read as a real rate move rather than a change of measurement."""

    late = _levels(1910, [4.0 + 0.01 * i for i in range(300)])
    # Same shape, offset by a constant, starting 120 months earlier.
    early = _levels(1900, [4.0 + 0.01 * (i - 120) + 0.5 for i in range(420)])

    spliced = splice_at_seam(early=early, late=late)

    assert len(spliced) == 420
    assert spliced[0].month == early[0].month
    assert spliced[-1].month == late[-1].month
    # Continuous across the seam: consecutive steps are the series' own 0.01, not 0.01 - 0.5.
    steps = [b.value - a.value for a, b in pairwise(spliced)]
    assert max(steps) == pytest.approx(0.01, abs=1e-9)
    assert min(steps) == pytest.approx(0.01, abs=1e-9)


def test_the_late_series_is_never_altered_by_the_splice() -> None:
    """It is the better measurement and the one every other fit uses; the early series is what
    bends to meet it. Shifting the modern half would silently change the fitted rate block."""

    late = _levels(1910, [4.0 + 0.02 * i for i in range(300)])
    early = _levels(1900, [9.0] * 420)

    spliced = {level.month: level.value for level in splice_at_seam(early=early, late=late)}

    assert all(spliced[level.month] == pytest.approx(level.value) for level in late)


def test_the_shift_comes_from_the_seam_not_the_whole_overlap() -> None:
    """The two series disagree by a time-VARYING amount, so a whole-overlap mean would import a
    late-period discrepancy into an early-period observation."""

    late = _levels(1910, [4.0] * 300)
    # Agrees at the seam, then drifts far apart later in the overlap.
    early = _levels(1900, [4.0] * 130 + [4.0 + 0.05 * i for i in range(290)])

    spliced = splice_at_seam(early=early, late=late)

    # Seam-anchored: the offset is ~0 there, so pre-seam values pass through essentially intact.
    assert spliced[0].value == pytest.approx(4.0, abs=0.02)


def test_too_little_overlap_to_anchor_is_rejected() -> None:
    late = _levels(1910, [4.0] * 300)
    early = _levels(1900, [4.5] * 125)  # only 5 months of overlap
    with pytest.raises(ValueError, match="fewer than the"):
        splice_at_seam(early=early, late=late)
