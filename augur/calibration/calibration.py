"""Compare any augur exogenous model's rollouts against prediction markets.

``run_calibration`` is a pure library function: it samples a :class:`Sampler`,
slices its private-equity bundle into per-rollout trajectories, resolves every
``exact`` catalog market apples-to-apples (``p_model`` + Wilson CI + unresolved
share vs the LIVE market price), and surfaces the rest (price + reason + an optional
related augur signal). It returns a typed :class:`CalibrationResult` and does NOT
print -- a CLI or backend renders it.

augur models EVENTS, not company valuation or revenue. Only event-based markets
(``ipo_by_date``, ``pre_ipo_failure``) are scored; valuation/revenue/etc. markets
are surfaced, never scored.

``p_market`` ALWAYS comes from live Manifold via an injected :class:`ManifoldClient`
(its TTL cache absorbs the repeated per-market reads of rapid auto-refreshes); tests
inject a hermetic ``MockTransport``-backed client.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date

import numpy as np
from pydantic import BaseModel
from statsmodels.stats.proportion import proportion_confint

from augur.calibration.catalog import CorrelateMarket, ExactMarket, MarketCatalog, SurfacedMarket
from augur.calibration.manifold import ManifoldClient, ManifoldMarket
from augur.calibration.resolvers import (
    Resolution,
    RolloutTrajectory,
    resolve_ipo_by_date,
    resolve_market,
    trajectories_from_bundle,
)
from augur.model.exogenous import ExogenousSamplingRequest, Sampler
from augur.model.private_equity_bundle import PrivateEquityBundle, PrivateEquityFloatChannel
from augur.model.series import IssuerId


def wilson_interval(yes: int, n: int) -> tuple[float, float]:
    """95% Wilson score confidence interval for a binomial proportion (NaNs if n == 0)."""
    if n == 0:
        return (math.nan, math.nan)
    low, high = proportion_confint(yes, n, alpha=0.05, method="wilson")
    return (float(low), float(high))


def kl_bits_market_vs_model(p_market: float, p_model: float) -> float:
    """`D_KL(market ‖ model)` for the two Bernoulli forecasts, in bits.

    The markets are unresolved, so there is no realized label to score against; we instead
    measure how far the model's forecast is from the market's, treating the live market price
    as the reference distribution. This is the reducible part of the cross-entropy (xent minus
    the market's own entropy) and is exactly what calibrating the model toward the market drives
    to zero — so it sorts cleanly by "loudest disagreement" (0 iff the forecasts match).

    The model probability is clamped off {0, 1} (a single unanimous rollout batch would otherwise
    send the divergence to +inf); the `0·log0 = 0` convention handles market endpoints.
    """
    eps = 1e-6
    q = min(1.0 - eps, max(eps, p_model))
    total = 0.0
    if p_market > 0.0:
        total += p_market * math.log2(p_market / q)
    if p_market < 1.0:
        total += (1.0 - p_market) * math.log2((1.0 - p_market) / (1.0 - q))
    return max(0.0, total)



class AugurContext(BaseModel):
    """A related (NOT equal) augur signal surfaced next to a market that isn't scored."""

    signal: str
    # `= None` (not bare `| None`) so the field is optional in the exported OpenAPI/Zod
    # schema, matching the server's `exclude_none=True` wire (None -> key dropped).
    p_model: float | None = None
    note: str


class CleanRow(BaseModel):
    """An apples-to-apples comparison: a market augur models as an event."""

    slug: str
    question: str
    # Canonical Manifold market page, fetched live alongside the price (the catalog stores
    # only a slug). The frontend links the question title to this URL.
    url: str
    p_market: float
    p_model: float | None = None  # None when no rollout resolved YES/NO within the horizon
    ci95: tuple[float, float]
    n_resolved: int
    unresolved: int
    # `D_KL(market ‖ model)` in bits: the model-vs-market disagreement we actually optimize.
    # None when no rollout resolved (p_model is None), matching p_model's nullability.
    kl_bits: float | None = None


class SurfacedRow(BaseModel):
    """A market augur lacks the concept for: shown with its price + reason, never scored."""

    slug: str
    question: str
    # Canonical Manifold market page, fetched live alongside the price (see CleanRow.url).
    # The frontend links the question title to this URL, same as the scored table.
    url: str
    mappability: str
    # Optional (`= None`) to match the endpoint's drop-None wire; see CleanRow above.
    correlate_of: str | None = None
    p_market: float
    reason: str | None = None
    augur_context: AugurContext | None = None


class CalibrationResult(BaseModel):
    issuer: str
    as_of: date
    horizon_months: int
    rollout_count: int
    clean: list[CleanRow]
    surfaced: list[SurfacedRow]


class MonthBand(BaseModel):
    """One month's percentile band of a float channel across rollouts."""

    month_index: int
    values: dict[str, float]  # percentile (as string, e.g. "50.0") -> channel value


class MarkFan(BaseModel):
    """Per-month percentile bands of a float channel (default the per-unit mark)."""

    issuer: str
    channel: str
    percentiles: list[float]
    months: list[MonthBand]


def _clean_row(market: ExactMarket, trajectories: list[RolloutTrajectory], manifold: ManifoldMarket) -> CleanRow:
    p_market = manifold.require_probability()
    counts = Counter(
        resolve_market(t, mapping_kind=market.mapping_kind, params=market.mapping_params) for t in trajectories
    )
    yes, no, unresolved = counts[Resolution.YES], counts[Resolution.NO], counts[Resolution.UNRESOLVED]
    n = yes + no
    p_model = yes / n if n else None
    return CleanRow(
        slug=market.slug,
        question=market.question,
        url=manifold.url,
        p_market=p_market,
        p_model=p_model,
        ci95=wilson_interval(yes, n),
        n_resolved=n,
        unresolved=unresolved,
        kl_bits=kl_bits_market_vs_model(p_market, p_model) if p_model is not None else None,
    )


def _augur_context(market: SurfacedMarket, trajectories: list[RolloutTrajectory]) -> AugurContext | None:
    """The nearest clean augur signal for a surfaced market, where one exists.

    Currently only the IPO-timing correlate: P(PUBLIC_MARKET_OPEN by the deadline)
    for correlate markets whose `correlate_of` is `ipo_by_date`.
    """
    if (
        not isinstance(market, CorrelateMarket)
        or market.correlate_of != "ipo_by_date"
        or market.resolution_deadline is None
        or not trajectories
    ):
        return None
    by_month = trajectories[0].month_on_or_before(market.resolution_deadline)
    counts = Counter(resolve_ipo_by_date(t, by_month=by_month) for t in trajectories)
    n = counts[Resolution.YES] + counts[Resolution.NO]
    return AugurContext(
        signal="P(PUBLIC_MARKET_OPEN by deadline)",
        p_model=counts[Resolution.YES] / n if n else None,
        note="related, NOT equal to this market",
    )


def _surfaced_row(
    market: SurfacedMarket, trajectories: list[RolloutTrajectory], manifold: ManifoldMarket
) -> SurfacedRow:
    return SurfacedRow(
        slug=market.slug,
        question=market.question,
        url=manifold.url,
        mappability=market.mappability,
        correlate_of=market.correlate_of if isinstance(market, CorrelateMarket) else None,
        p_market=manifold.require_probability(),
        reason=" ".join(market.reason.split()) if market.reason else None,
        augur_context=_augur_context(market, trajectories),
    )


def mark_fan(
    bundle: PrivateEquityBundle,
    *,
    issuer: IssuerId | str,
    rollout_count: int,
    horizon_months: int,
    percentiles: tuple[float, ...],
    channel: PrivateEquityFloatChannel = PrivateEquityFloatChannel.MARK_USD_PER_UNIT,
) -> MarkFan:
    """Per-month percentile bands of a float channel across rollouts (for a fan chart).

    Serializes cleanly to JSON: `months[i].values` maps each percentile (stringified)
    to the channel value at that month. Defaults to the per-unit mark.
    """
    matrix = bundle.issuer_float_matrix(issuer, channel, rollout_count=rollout_count, horizon_months=horizon_months)
    bands = np.percentile(matrix, percentiles, axis=0)  # (len(percentiles), horizon+1)
    months = [
        MonthBand(month_index=month, values={str(p): float(bands[i, month]) for i, p in enumerate(percentiles)})
        for month in range(horizon_months + 1)
    ]
    return MarkFan(issuer=str(issuer), channel=channel, percentiles=list(percentiles), months=months)


def sample_private_equity_bundle(
    model: Sampler, *, issuer: str, horizon_months: int, rollout_seeds: tuple[int, ...]
) -> PrivateEquityBundle:
    """Sample `model`'s private-equity bundle for one issuer over a horizon.

    Factored out of `run_calibration` so a caller can reuse the same sampled bundle
    for both the market scoring and a `mark_fan` (the bundle is the only thing both
    need), instead of paying for two independent rollouts.
    """
    request = ExogenousSamplingRequest(
        horizon_months=horizon_months,
        rollout_seeds=rollout_seeds,
        required_private_equity_issuers=frozenset({IssuerId(issuer)}),
    )
    return model.sample(request).private_equity


def run_calibration(
    model: Sampler,
    catalog: MarketCatalog,
    *,
    issuer: str,
    horizon_months: int,
    rollout_seeds: tuple[int, ...],
    price_client: ManifoldClient | None = None,
    bundle: PrivateEquityBundle | None = None,
) -> CalibrationResult:
    """Score an exogenous model's rollouts against a curated prediction-market catalog.

    Samples `model` for `issuer` over `horizon_months`, resolves every `exact`
    market apples-to-apples, and surfaces the rest. Each market's `p_market` is fetched
    LIVE per market via `price_client` (a real `ManifoldClient` by default, whose TTL cache
    absorbs the repeated reads of rapid auto-refreshes; tests inject a hermetic client).

    Pass a pre-sampled `bundle` (from `sample_private_equity_bundle` with the same
    issuer/horizon/seeds) to reuse one rollout for both scoring and a `mark_fan`; when
    omitted, `model` is sampled here.
    """
    if price_client is None:
        price_client = ManifoldClient()
    as_of = catalog.metadata.model_anchor_date
    rollout_count = len(rollout_seeds)
    if bundle is None:
        bundle = sample_private_equity_bundle(
            model, issuer=issuer, horizon_months=horizon_months, rollout_seeds=rollout_seeds
        )
    trajectories = list(
        trajectories_from_bundle(
            bundle, issuer=issuer, rollout_count=rollout_count, horizon_months=horizon_months, as_of=as_of
        )
    )

    return CalibrationResult(
        issuer=issuer,
        as_of=as_of,
        horizon_months=horizon_months,
        rollout_count=rollout_count,
        clean=[
            _clean_row(market, trajectories, price_client.get_market(market.manifold_id))
            for market in catalog.exact_markets()
        ],
        surfaced=[
            _surfaced_row(market, trajectories, price_client.get_market(market.manifold_id))
            for market in catalog.surfaced_markets()
        ],
    )
