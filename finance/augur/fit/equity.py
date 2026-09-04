"""Fit the structural macro provider's equity block: log-return drift/volatility (`LogReturnFit`,
`fit_log_returns`), and its rate coupling (`RateBetaFit`, `fit_rate_beta`) — the model's only
cross-block parameter, since a covariance is undefined where series do not overlap and every
other parameter here is a marginal fitted on its own longest history. See
`fit_structural_macro_defaults` in `structural_macro.py` for how the two combine, and why.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

import numpy as np

from finance.augur.model.structural_macro import MINIMUM_MONTHS, MONTHS_PER_YEAR
from finance.evidence.loading import MonthlyLevel


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
    weaker estimate but an undefined one. `short_rate` must already be decimal-scale (not FRED
    percent) — this function does no unit conversion of its own.
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
