"""Fit a per-rollout dilution prior from paired price + valuation observations.

This is the M2.2-A fit half. It turns an issuer's paired per-share prices and company
post-money valuations (the `augur.fit.private_equity` observation types -- the same
`PriceObservation` / `ValuationObservation` the `train_private_equity` binary ingests) into
the dilution config block consumed by `PrivateEquityRiskIssuerConfig`:

    annual_dilution_rate            (median per-rollout dilution rate)
    annual_dilution_rate_log_sigma  (per-rollout dilution-rate dispersion)

and, optionally, a refreshed `valuation_monthly_log_return_mu` / `_sigma` from the valuation
series alone.

Method -- implied-shares log-linear regression. For each date carrying BOTH a price and a
(near-dated) valuation, the implied share count is

    implied_shares = valuation_usd / price_usd_per_share

(robust to primary-vs-secondary: it is a per-date ratio, so a primary round that both mints
shares and resets the post-money still lands on the same shares-vs-time curve; the
primary/secondary refinement, M2.2-C, mainly affects *when* shares step, not the overall
slope). We then fit, by ordinary least squares,

    log(implied_shares) ~ log(shares0) + (delta_years) * log(1 + r)

with delta_years measured from the first paired date, and recover

    annual_dilution_rate = exp(slope_per_year) - 1.

The residual scatter of `log(implied_shares)` about the fit line gives the per-rollout
dispersion `annual_dilution_rate_log_sigma` (see `_log_sigma_from_residuals`). This mirrors
the implied-share-count idea already used by `private_equity._estimate_current_market_cap`,
but fits the *slope over time* (dilution) rather than collapsing to a single point estimate.

HONEST LIMITATIONS (see augur/plans/prediction_market_calibration.md, M2.2 section):

* Point/log-linear, not Bayesian -- OLS point estimates, no posterior. A full NUTS fit over
  (rate, sigma, V-drift, V-vol) jointly is DEFERRED to M2.2-D.
* The discrete primary-round event kind (a raise as a discrete share-count step + post-money
  reset) is DEFERRED to M2.2-C; this fit treats dilution as smooth/continuous.
* With ~5 paired points the dispersion is WEAKLY IDENTIFIED and intentionally wide; treat
  `annual_dilution_rate_log_sigma` as a wide prior, not a precise estimate.

Generic -- no issuer specifics; takes observations, returns parameters.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from itertools import pairwise

from finance.augur.fit.private_equity import PriceObservation, ValuationObservation

# Default window for pairing a price observation with a valuation observation on a "near"
# date. Tender prices and post-money valuations are rarely struck on the exact same calendar
# day; a month of slack pairs a round's price with its post-money.
_DEFAULT_PAIRING_TOLERANCE_DAYS = 31

_DAYS_PER_YEAR = 365.25
_MONTHS_PER_YEAR = 12.0
_DAYS_PER_MONTH = _DAYS_PER_YEAR / _MONTHS_PER_YEAR


@dataclass(frozen=True)
class ImpliedSharePoint:
    """One paired observation: implied shares = valuation / price on (near-)coincident dates."""

    date: dt.date
    price_usd_per_share: float
    valuation_usd: float
    implied_shares: float
    delta_years: float


@dataclass(frozen=True)
class DilutionPrior:
    """Fitted dilution prior + the implied-share points the fit used (for transparency).

    `valuation_monthly_log_return_mu` / `_sigma` are `None` unless the valuation series alone
    had enough observations to estimate drift/vol from (kept clearly separable from the
    implied-shares dilution fit).
    """

    annual_dilution_rate: float
    annual_dilution_rate_log_sigma: float
    implied_share_points: tuple[ImpliedSharePoint, ...]
    shares0: float
    residual_log_std: float
    valuation_monthly_log_return_mu: float | None = None
    valuation_monthly_log_return_sigma: float | None = None


def _pair_price_and_valuation(
    prices: list[PriceObservation], valuations: list[ValuationObservation], *, tolerance_days: int
) -> list[tuple[dt.date, float, float]]:
    """Pair each price with its nearest-dated valuation within `tolerance_days`.

    Returns `(date, price_usd_per_share, valuation_usd)` triples (the price's date),
    deduplicated by date and sorted. A price with no valuation inside the window is dropped.
    """

    if not valuations:
        return []
    # date -> (gap_days, valuation_usd, price); on a duplicate price date keep the closer match.
    best: dict[dt.date, tuple[int, float, float]] = {}
    for price in prices:
        nearest = min(valuations, key=lambda val: abs((val.observed_at - price.observed_at).days))
        gap = abs((nearest.observed_at - price.observed_at).days)
        if gap > tolerance_days:
            continue
        existing = best.get(price.observed_at)
        if existing is None or gap < existing[0]:
            best[price.observed_at] = (gap, nearest.valuation_usd, price.price_usd_per_share)
    return sorted((date, price, valuation_usd) for date, (_gap, valuation_usd, price) in best.items())


def _ordinary_least_squares(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """OLS of `ys` on `xs`. Returns (intercept, slope, residual_std).

    `residual_std` is the std of residuals about the fit line with the n-2 OLS denominator
    when there are >= 3 points, else 0.0 -- a 2-point line is exact, so residuals are all zero.
    Caller guarantees `len(xs) == len(ys) >= 2` and that `xs` are not all identical.
    """

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    if n < 3:
        return intercept, slope, 0.0
    residual_var = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True)) / (n - 2)
    return intercept, slope, math.sqrt(residual_var)


def _log_sigma_from_residuals(*, residual_log_std: float, xs: list[float], annual_dilution_rate: float) -> float:
    """Convert log-shares residual scatter into a per-rollout log-sigma on the rate.

    The OLS standard error of the slope `b = log(1 + r)` is

        se_b = residual_log_std / sqrt(sum (delta_years_i - mean delta_years)^2).

    `annual_dilution_rate_log_sigma` is the sigma of `log(r)` in the sampler draw
    `r = median * exp(sigma * z)`. Mapping the slope's standard error from `b`-space to
    `log(r)`-space by the delta method (`d log r = (1 + r)/r * db`, since `dr = (1 + r) db`)
    gives

        sigma_logr ~ se_b * (1 + r) / r.

    This is a rough, WEAKLY-IDENTIFIED conversion with ~5 points and is intentionally wide
    (see module docstring / the M2.2 plan). When `r <= 0` the median dilution is zero, so any
    LogNormal dispersion around it is moot (`0 * exp(sigma z) == 0`); we return 0.0.
    """

    if annual_dilution_rate <= 0.0 or residual_log_std <= 0.0:
        return 0.0
    mean_x = sum(xs) / len(xs)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0.0:
        return 0.0
    se_slope = residual_log_std / math.sqrt(sxx)
    return se_slope * (1.0 + annual_dilution_rate) / annual_dilution_rate


def _fit_valuation_drift_and_vol(valuations: list[ValuationObservation]) -> tuple[float | None, float | None]:
    """Refresh monthly log-return drift + vol from the valuation series alone.

    Kept clearly separable from the implied-shares dilution fit: reads ONLY the valuation
    observations. Returns `(mu, sigma)`, both `None` when there are < 2 valuations (no return
    to measure), and `sigma=None` when there are < 3 (a single return has no scatter).
    """

    if len(valuations) < 2:
        return None, None
    ordered = sorted(valuations, key=lambda obs: obs.observed_at)
    monthly_log_returns = [
        math.log(later.valuation_usd / earlier.valuation_usd)
        / max(1.0, (later.observed_at - earlier.observed_at).days / _DAYS_PER_MONTH)
        for earlier, later in pairwise(ordered)
    ]
    mu = sum(monthly_log_returns) / len(monthly_log_returns)
    if len(monthly_log_returns) < 2:
        return mu, None
    variance = sum((x - mu) ** 2 for x in monthly_log_returns) / (len(monthly_log_returns) - 1)
    return mu, math.sqrt(variance)


def fit_dilution_prior(
    prices: list[PriceObservation],
    valuations: list[ValuationObservation],
    *,
    pairing_tolerance_days: int = _DEFAULT_PAIRING_TOLERANCE_DAYS,
    refresh_valuation_drift: bool = True,
) -> DilutionPrior:
    """Fit a per-rollout dilution prior from paired price + valuation observations.

    Raises `ValueError` when fewer than two paired (price, valuation) points exist -- a single
    point cannot identify a slope.
    """

    triples = _pair_price_and_valuation(prices, valuations, tolerance_days=pairing_tolerance_days)
    if len(triples) < 2:
        raise ValueError(
            f"need >= 2 paired (price, valuation) points within {pairing_tolerance_days} days to fit a dilution "
            f"slope; got {len(triples)}"
        )

    first_date = triples[0][0]
    xs = [(date - first_date).days / _DAYS_PER_YEAR for date, _price, _valuation in triples]
    implied_shares = [valuation / price for _date, price, valuation in triples]
    ys = [math.log(s) for s in implied_shares]

    intercept, slope, residual_log_std = _ordinary_least_squares(xs, ys)
    annual_dilution_rate = math.exp(slope) - 1.0
    annual_dilution_rate_log_sigma = _log_sigma_from_residuals(
        residual_log_std=residual_log_std, xs=xs, annual_dilution_rate=annual_dilution_rate
    )

    points = tuple(
        ImpliedSharePoint(
            date=date, price_usd_per_share=price, valuation_usd=valuation, implied_shares=shares, delta_years=x
        )
        for (date, price, valuation), shares, x in zip(triples, implied_shares, xs, strict=True)
    )

    valuation_mu: float | None = None
    valuation_sigma: float | None = None
    if refresh_valuation_drift:
        valuation_mu, valuation_sigma = _fit_valuation_drift_and_vol(valuations)

    return DilutionPrior(
        annual_dilution_rate=annual_dilution_rate,
        annual_dilution_rate_log_sigma=annual_dilution_rate_log_sigma,
        implied_share_points=points,
        shares0=math.exp(intercept),
        residual_log_std=residual_log_std,
        valuation_monthly_log_return_mu=valuation_mu,
        valuation_monthly_log_return_sigma=valuation_sigma,
    )
