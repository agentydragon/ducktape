"""Fit the structural macro provider's joint state: a VAR(1) on `(short rate, term spread,
inflation rate)`, from FRED `FEDFUNDS`, `GS10` and `CPIAUCSL`. This is the fit
`fit_structural_macro_defaults` (in `structural_macro.py`) uses for the provider's shipped
macro state — see that module's docstring for why it is fitted on its own window.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from finance.augur.model.structural_macro import MINIMUM_MONTHS, PERCENT_TO_DECIMAL, MacroStateMatrix, MacroStateVector
from finance.evidence.loading import MonthlyLevel

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
