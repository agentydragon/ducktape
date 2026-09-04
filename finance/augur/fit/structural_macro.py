"""Fit the structural macro provider's latent rates block from FRED evidence.

Separable from the joint fit on purpose, and this is the whole reason the provider's state is
two rates rather than a factor block. The joint VECM/state-space fit inner-joins every series
into ONE aligned window, so adding a 1954 series there would not buy 1954 — it would be
truncated to whatever the shortest series allows (ETH, ~2017). The rates block shares no
window with anything: `FEDFUNDS` (1954-07) and `GS10` (1953-04) are fitted against each other
and nothing else, and 70 years is exactly what makes the fit worth doing.

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
from itertools import pairwise
from pathlib import Path

import numpy as np

from finance.augur.model.structural_macro import (
    MONTHS_PER_YEAR,
    FitWindowProvenance,
    MacroStateMatrix,
    MacroStateVector,
    MacroVarSpec,
    StructuralMacroFittedDefaults,
)
from finance.evidence.loading import MonthlyLevel, read_french_market_levels, read_monthly_levels
from finance.evidence.sources import FRED_CPI, FRED_FEDFUNDS, FRED_GS10, FRENCH_FACTORS, YAHOO_VFINX

# FRED publishes both rate series in PERCENT; every rate inside augur is a decimal.
PERCENT_TO_DECIMAL = 0.01

# A fit needs enough months that `1 - b` is not noise. 240 (20 years) is well below the ~865
# the real series carry and well above anything that could fit a single regime.
MINIMUM_MONTHS = 240


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


# ── Log-return blocks: equity and inflation ──────────────────────────────────────────────

# Each block is fitted on its OWN longest window rather than on one window shared by all of
# them. That is the payoff of a structural model over a covariance matrix, and it is why this
# provider carries no crypto: the joint VECM fit inner-joins everything and ETH truncates it to
# ~2017, but nothing here has to share a window with anything it is not correlated to.
#
# The rule, stated so it can be argued with: a MARGINAL (a drift, a volatility) is fitted on
# its own longest history; a CROSS-BLOCK parameter must use the common window, because a
# covariance is undefined where the series do not overlap. `fit_rate_beta` is the only
# cross-block parameter in the model, so it is the only thing paying the truncation.
#
# The hazard this leaves, named rather than hidden: inflation reaches back to 1947 and equity
# only to 1993, so the model's implied REAL equity return pairs a sample containing the 1970s
# with one that does not. It lands at ~7.3%/yr real, close to the long-run realized figure, so
# the mismatch is not currently doing damage — but it is a coincidence, not a control.


@dataclass(frozen=True)
class LogReturnFit:
    """A geometric process's fitted monthly log-return drift and volatility."""

    monthly_log_mu: float
    monthly_log_sigma: float
    sample_months: int
    first_month: date
    last_month: date

    @property
    def annualized_nominal_return(self) -> float:
        return float(np.expm1(self.monthly_log_mu * MONTHS_PER_YEAR))

    @property
    def annualized_volatility(self) -> float:
        return float(np.sqrt(MONTHS_PER_YEAR) * self.monthly_log_sigma)


def fit_log_returns(levels: Sequence[MonthlyLevel]) -> LogReturnFit:
    """Fit a level series' monthly log returns. Levels must be strictly positive."""

    if len(levels) < MINIMUM_MONTHS:
        raise ValueError(f"need at least {MINIMUM_MONTHS} monthly observations; got {len(levels)}")
    months = [level.month for level in levels]
    if months != sorted(months):
        raise ValueError("monthly levels must be sorted oldest first")
    values = np.array([level.value for level in levels], dtype=np.float64)
    if not np.all(values > 0.0):
        raise ValueError("log returns need strictly positive levels")

    returns = np.diff(np.log(values))
    return LogReturnFit(
        monthly_log_mu=float(np.mean(returns)),
        monthly_log_sigma=float(np.std(returns, ddof=1)),
        sample_months=len(levels),
        first_month=months[0],
        last_month=months[-1],
    )


@dataclass(frozen=True)
class RateBetaFit:
    """Equity's loading on the CHANGE in the short rate, and how little it explains.

    `r_squared` is not decoration. It is the field that decides whether `beta` should be used
    at all, and on the real data it is ~0.004 — so the honest reading is that this model has no
    measurable contemporaneous equity/rates coupling at monthly frequency, and a study that
    turns on bond/equity correlation is not answered by it.
    """

    beta: float
    r_squared: float
    sample_months: int
    first_month: date
    last_month: date


def fit_rate_beta(*, equity_levels: Sequence[MonthlyLevel], short_rate: Sequence[MonthlyLevel]) -> RateBetaFit:
    """Regress equity's monthly log return on the same month's change in the short rate.

    Inner-joined, so this necessarily runs on the COMMON window — the one place in the model
    where the shortest series sets the sample, because a covariance off a non-overlap is not a
    weaker estimate but an undefined one.
    """

    equity_by_month = {level.month: level.value for level in equity_levels}
    rate_by_month = {level.month: level.value for level in short_rate}
    months = sorted(set(equity_by_month) & set(rate_by_month))
    if len(months) < MINIMUM_MONTHS:
        raise ValueError(f"need at least {MINIMUM_MONTHS} shared months; got {len(months)}")

    returns = np.array([math.log(equity_by_month[b] / equity_by_month[a]) for a, b in pairwise(months)])
    rate_changes = np.array([rate_by_month[b] - rate_by_month[a] for a, b in pairwise(months)])
    slope, intercept = np.polyfit(rate_changes, returns, 1)
    residuals = returns - (intercept + slope * rate_changes)
    total = float(np.var(returns))
    return RateBetaFit(
        beta=float(slope),
        r_squared=0.0 if total == 0.0 else 1.0 - float(np.var(residuals)) / total,
        sample_months=len(months),
        first_month=months[0],
        last_month=months[-1],
    )


# ── The joint macro block: a VAR(1) on (short rate, term spread, inflation) ───────────────

MACRO_STATE_NAMES = ("short_rate", "term_spread", "inflation_rate")
MACRO_STATE_DIM = len(MACRO_STATE_NAMES)


@dataclass(frozen=True)
class MacroVarFit:
    """A fitted VAR(1): `x[t] = c + A x[t-1] + L z[t]`, `z ~ N(0, I)`.

    Replaces three independent processes with one, because they are not independent and the
    ways they are coupled are the ways the answer moves. Two in particular:

    - **Persistence.** Inflation as an i.i.d. shock around a fixed drift makes the 30-year
      price level nearly deterministic (a 1-sigma band 4.8x narrower than history delivered).
      Here inflation is a STATE with its own lag, so a decade of high inflation is reachable.
    - **The Fed reaction.** The short rate loads on lagged inflation, so the state that erodes
      a CPI-indexed spend is also the state that raises what new bonds pay. Without it, a bond
      sleeve can be caught by inflation with no mechanism to ever catch up — which is exactly
      the failure the independent version produced.

    `L` is the lower-triangular Cholesky factor of the innovation covariance, so a single draw
    of independent normals produces correctly correlated shocks. Correlation across equations
    matters on its own: a rate surprise and an inflation surprise arrive together.
    """

    # MacroStateVector/Matrix are fixed at 3 (MACRO_STATE_DIM): this is always the
    # (short_rate, term_spread, inflation_rate) VAR, not a generic N-state fitter.
    intercept: MacroStateVector
    transition: MacroStateMatrix
    shock_cholesky: MacroStateMatrix
    latest_state: MacroStateVector
    first_month: date
    latest_month: date
    sample_months: int

    @property
    def spectral_radius(self) -> float:
        """Largest eigenvalue modulus of `A`. Below 1 means the state is stationary."""

        return float(np.max(np.abs(np.linalg.eigvals(np.asarray(self.transition)))))

    @property
    def long_run_mean(self) -> MacroStateVector:
        """`(I - A)^-1 c` — where the state settles absent shocks."""

        transition = np.asarray(self.transition)
        return _as_vector3(np.linalg.solve(np.eye(MACRO_STATE_DIM) - transition, np.asarray(self.intercept)).tolist())

    @property
    def inflation_pass_through(self) -> float:
        """Long-run rise in the short rate per point of permanently higher inflation.

        The Taylor principle says a stable policy rule has this ABOVE ONE — the nominal rate
        must outrun inflation or real rates fall as inflation rises, which is destabilizing.
        Nothing here imposes that; it is a property of the fit, and therefore a check on it.
        """

        own_lag = self.transition[0][0]
        return self.transition[0][2] / (1.0 - own_lag)


def _as_vector3(values: Sequence[float]) -> MacroStateVector:
    """A numpy row as a genuine 3-tuple: unpacking (rather than `tuple(values)`) is what
    gives mypy a fixed-arity result, and raises immediately on a malformed row."""
    a, b, c = values
    return (a, b, c)


def _as_matrix3(rows: Sequence[Sequence[float]]) -> MacroStateMatrix:
    row0, row1, row2 = rows
    return (_as_vector3(row0), _as_vector3(row1), _as_vector3(row2))


def fit_macro_var(
    *,
    short_rate_percent: Sequence[MonthlyLevel],
    long_rate_percent: Sequence[MonthlyLevel],
    cpi_level: Sequence[MonthlyLevel],
    inflation_lookback_months: int = 12,
) -> MacroVarFit:
    """Fit the joint macro state on FRED `FEDFUNDS`, `GS10` and `CPIAUCSL`.

    The inflation state is TRAILING-YEAR log inflation, not the month-over-month rate. The
    month-over-month rate is mostly measurement noise (its own lag is ~0.48, against ~0.98
    here), and what a bond sleeve and a central bank both respond to is the regime rather than
    one print. The simulator reads inflation as an annual reset anyway, so nothing downstream
    wants the monthly wiggle this discards.
    """

    short = {level.month: level.value * PERCENT_TO_DECIMAL for level in short_rate_percent}
    long_rate = {level.month: level.value * PERCENT_TO_DECIMAL for level in long_rate_percent}
    cpi = {level.month: level.value for level in cpi_level}
    months = sorted(set(short) & set(long_rate) & set(cpi))
    if len(months) < MINIMUM_MONTHS + inflation_lookback_months:
        raise ValueError(f"need at least {MINIMUM_MONTHS + inflation_lookback_months} shared months")

    usable = [
        month
        for index, month in enumerate(months)
        if index >= inflation_lookback_months and cpi[months[index - inflation_lookback_months]] > 0.0
    ]
    states = np.array(
        [
            [
                short[month],
                long_rate[month] - short[month],
                math.log(cpi[month] / cpi[months[months.index(month) - inflation_lookback_months]]),
            ]
            for month in usable
        ],
        dtype=np.float64,
    )

    previous, current = states[:-1], states[1:]
    design = np.column_stack([np.ones(len(previous)), previous])
    # One OLS per equation. For a VAR whose equations share the same regressors this is exactly
    # the joint estimate, so nothing is lost by fitting them separately.
    coefficients, *_ = np.linalg.lstsq(design, current, rcond=None)
    residuals = current - design @ coefficients
    covariance = residuals.T @ residuals / (len(residuals) - design.shape[1])

    return MacroVarFit(
        intercept=_as_vector3(coefficients[0].tolist()),
        transition=_as_matrix3(coefficients[1:].T.tolist()),
        shock_cholesky=_as_matrix3(np.linalg.cholesky(covariance).tolist()),
        latest_state=_as_vector3(states[-1].tolist()),
        first_month=usable[0],
        latest_month=usable[-1],
        sample_months=len(usable),
    )


# ── Offline entry point: the checked-in fit ───────────────────────────────────────────────


def fit_structural_macro_defaults(evidence_dir: Path) -> StructuralMacroFittedDefaults:
    """Fit `structural_macro`'s checked-in defaults from real evidence: the joint macro VAR
    and the equity log-return / rate-beta blocks, each on its own longest window (module
    docstring; § Log-return blocks above) rather than the joint fit's shared aligned window.
    """
    macro_fit = fit_macro_var(
        short_rate_percent=read_monthly_levels(evidence_dir, FRED_FEDFUNDS),
        long_rate_percent=read_monthly_levels(evidence_dir, FRED_GS10),
        cpi_level=read_monthly_levels(evidence_dir, FRED_CPI),
    )
    equity_fit = fit_log_returns(read_french_market_levels(evidence_dir, FRENCH_FACTORS))
    beta_fit = fit_rate_beta(
        equity_levels=read_monthly_levels(evidence_dir, YAHOO_VFINX),
        short_rate=read_monthly_levels(evidence_dir, FRED_FEDFUNDS),
    )

    return StructuralMacroFittedDefaults(
        macro_state=MacroVarSpec(
            initial_state=macro_fit.latest_state,
            intercept=macro_fit.intercept,
            transition=macro_fit.transition,
            shock_cholesky=macro_fit.shock_cholesky,
        ),
        macro_state_fit=FitWindowProvenance(
            source=f"{FRED_FEDFUNDS.provenance_label},{FRED_GS10.provenance_label},{FRED_CPI.provenance_label}",
            first_month=macro_fit.first_month,
            last_month=macro_fit.latest_month,
            sample_months=macro_fit.sample_months,
        ),
        equity_monthly_log_return_mu=equity_fit.monthly_log_mu,
        equity_monthly_log_return_sigma=equity_fit.monthly_log_sigma,
        equity_fit=FitWindowProvenance(
            source=FRENCH_FACTORS.provenance_label,
            first_month=equity_fit.first_month,
            last_month=equity_fit.last_month,
            sample_months=equity_fit.sample_months,
        ),
        rate_beta_fit=FitWindowProvenance(
            source=f"{YAHOO_VFINX.provenance_label},{FRED_FEDFUNDS.provenance_label}",
            first_month=beta_fit.first_month,
            last_month=beta_fit.last_month,
            sample_months=beta_fit.sample_months,
        ),
        rate_beta_fitted_value=beta_fit.beta,
        rate_beta_r_squared=beta_fit.r_squared,
    )
