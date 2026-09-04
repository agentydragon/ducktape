"""Fit a rate as an independent Ornstein-Uhlenbeck process, and the two-rate block built from it.

Not currently wired into `structural_macro`'s shipped fit — `fit_macro_var` (`macro_var.py`)
supersedes this with a JOINT VAR(1) that also captures inflation and the coupling between all
three series, which is what the shipped provider actually uses (see `structural_macro.py`'s
module docstring for why). Kept as a simpler, self-contained alternative: each rate fitted
independently, with no shared-window requirement at all — unlike the joint VECM/state-space
fit, which inner-joins every series into one aligned window and would truncate `FEDFUNDS`/
`GS10`'s 70-year overlap to whatever the shortest series allows.

**What the fit is.** Each rate is an Ornstein-Uhlenbeck process in discrete monthly steps,
which is an AR(1) in levels: `x[t] = a + b·x[t-1] + e`. So `reversion = 1 - b`,
`mean = a / (1 - b)`, `sigma = sd(e)`. On the LEVEL rather than the log, because a rate
legitimately reaches zero and a spread legitimately inverts.

**What the fit is not.** OLS on a near-unit-root series is biased, and knowing that changes
how the outputs should be read:

- `reversion` is biased UP (Kendall/Marriott-Pope): `b` is pulled toward zero in finite
  samples, so the fitted pull looks faster than it is. At `b ≈ 0.99` over 865 months the bias
  is roughly `(1 + 3b) / n` ≈ 0.005/month against a fitted 0.010 — the same order as the
  estimate. Read the half-life as "years, not months", not as a number.
- `mean` is the worst-identified parameter by far, because it is `a / (1 - b)` and the
  denominator is a small difference of two things near one. Over 1954-2026 the fitted short
  rate mean is ~4.9%; over 1990-2026 it is ~1.7%. Neither is wrong — the sample simply does
  not pin it, and a run that turns on the mean should sweep it rather than trust it.
- `sigma` is the best-identified of the three and the one that most changes the answer,
  since it is what makes a rate path a risk rather than a forecast.

`fit_rates_block`'s `window_start` is therefore a stated modeling choice, not a detail: it
chooses which regimes the fit has seen, and it is the single input that moves the mean most.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from finance.augur.model.structural_macro import MINIMUM_MONTHS, MONTHS_PER_YEAR, PERCENT_TO_DECIMAL
from finance.evidence.loading import MonthlyLevel


@dataclass(frozen=True)
class OrnsteinUhlenbeckFit:
    """One rate's fitted AR(1), plus the last observed level.

    `latest_level` is not a fitted quantity — it is where the process starts, which is a fact
    about today rather than about the sample. It is here because the caller needs both and
    separating them would let a config pair a 2026 initial level with a stale fit.
    """

    reversion_per_month: float
    long_run_mean: float
    monthly_sigma: float
    latest_level: float
    latest_month: date
    sample_months: int

    @property
    def half_life_years(self) -> float:
        """How long the process takes to close half a gap to its mean. `inf` if it never does.

        The DISCRETE half-life: the gap decays by `(1 - k)` per month, so it halves after
        `log(0.5) / log(1 - k)` months. Not the continuous-time `log(2) / k`, which is the
        same thing only in the limit and runs ~0.5% long at these speeds. Small, but the
        provider's own fund-convergence term is exact-discrete, and one of the two being an
        approximation of the other is the kind of thing that never gets noticed.
        """

        if self.reversion_per_month <= 0.0:
            return math.inf
        if self.reversion_per_month >= 1.0:
            return 0.0
        return math.log(0.5) / math.log(1.0 - self.reversion_per_month) / MONTHS_PER_YEAR


@dataclass(frozen=True)
class RatesBlockFit:
    """The provider's whole latent state, fitted.

    The term spread is fitted on `GS10 - FEDFUNDS` rather than on `GS10` directly, matching
    what the provider samples. Fitting the two rate LEVELS independently and subtracting would
    put no constraint on the spread at all, and the spread is what prices duration.
    """

    short_rate: OrnsteinUhlenbeckFit
    term_spread: OrnsteinUhlenbeckFit
    window_start: date


def fit_ornstein_uhlenbeck(levels: Sequence[MonthlyLevel]) -> OrnsteinUhlenbeckFit:
    """Fit `x[t] = a + b·x[t-1] + e` by OLS and report it as `(reversion, mean, sigma)`."""

    if len(levels) < MINIMUM_MONTHS:
        raise ValueError(f"need at least {MINIMUM_MONTHS} monthly observations to fit a rate; got {len(levels)}")
    months = [level.month for level in levels]
    if months != sorted(months):
        raise ValueError("monthly levels must be sorted oldest first")

    values = np.array([level.value for level in levels], dtype=np.float64)
    previous, current = values[:-1], values[1:]
    slope, intercept = np.polyfit(previous, current, 1)
    reversion = 1.0 - float(slope)
    if reversion <= 0.0:
        # b >= 1 leaves `a / (1 - b)` undefined or negative, and a sampler built from it would
        # drift without bound over 30 years. Worth rejecting, but do not read it as a unit-root
        # test: a SAMPLED random walk lands at b slightly below one — the same finite-sample
        # bias documented above — and sails through here as a slowly-reverting process. This
        # catches the degenerate case; nothing here can tell a slow reverter from a walk.
        raise ValueError(f"fitted AR(1) coefficient {slope:.4f} is not mean-reverting; no long-run mean exists")
    residuals = current - (intercept + slope * previous)
    return OrnsteinUhlenbeckFit(
        reversion_per_month=reversion,
        long_run_mean=float(intercept) / reversion,
        # ddof=2 for the two estimated parameters; at n≈865 it changes nothing, and at the
        # 240-month floor it is the difference between an estimate and a slightly wrong one.
        monthly_sigma=float(np.std(residuals, ddof=2)),
        latest_level=float(values[-1]),
        latest_month=levels[-1].month,
        sample_months=len(levels),
    )


def fit_rates_block(
    *, short_rate_percent: Sequence[MonthlyLevel], long_rate_percent: Sequence[MonthlyLevel], window_start: date
) -> RatesBlockFit:
    """Fit both latent rates from FRED's percent-denominated `FEDFUNDS` and `GS10`.

    Inner-joined on month so the spread is a real contemporaneous difference. The two series
    start eight months apart and both run to the present, so the join costs almost nothing
    here — but a silent misalignment would put a spread on the wrong month, and a spread of two
    near-identical numbers is exactly where that would not show up in the level.
    """

    short_by_month = {level.month: level.value * PERCENT_TO_DECIMAL for level in short_rate_percent}
    long_by_month = {level.month: level.value * PERCENT_TO_DECIMAL for level in long_rate_percent}
    shared = sorted(set(short_by_month) & set(long_by_month))
    months = [month for month in shared if month >= window_start]
    if not months:
        raise ValueError(f"no months shared by both rate series at or after {window_start}")

    return RatesBlockFit(
        short_rate=fit_ornstein_uhlenbeck([MonthlyLevel(month=m, value=short_by_month[m]) for m in months]),
        term_spread=fit_ornstein_uhlenbeck(
            [MonthlyLevel(month=m, value=long_by_month[m] - short_by_month[m]) for m in months]
        ),
        window_start=months[0],
    )
