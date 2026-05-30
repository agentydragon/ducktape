"""Compare any augur exogenous model's rollouts against prediction markets.

``run_calibration`` is a pure library function: it samples a :class:`Sampler`,
slices its private-equity bundle into per-rollout trajectories, resolves every
``exact`` catalog market apples-to-apples (``p_model`` + Wilson CI + unresolved
share vs the market price), and surfaces the rest (price + reason + an optional
related augur signal). It returns a typed :class:`CalibrationResult` and does NOT
print -- a CLI or backend renders it.

augur models EVENTS, not company valuation or revenue. Only event-based markets
(``ipo_by_date``, ``pre_ipo_failure``) are scored; valuation/revenue/etc. markets
are surfaced, never scored.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date

import numpy as np
from pydantic import BaseModel

from augur.calibration.catalog import MarketCatalog, MarketSpec
from augur.calibration.manifold import fetch_yes_probabilities
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


def wilson_interval(yes: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion (NaNs if n == 0)."""
    if n == 0:
        return (math.nan, math.nan)
    p = yes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


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
    mapping_kind: str
    # The nullable fields default to None so the generated Zod schema treats them as
    # optional. The endpoint drops None-valued keys (`exclude_none=True`); without a
    # default the codegen emits required `.nullable()` and rejects the omitted key.
    resolution_deadline: date | None = None
    p_market: float
    p_model: float | None = None  # None when no rollout resolved YES/NO within the horizon
    ci95: tuple[float, float]
    n_resolved: int
    unresolved: int
    abs_gap: float | None = None


class SurfacedRow(BaseModel):
    """A market augur lacks the concept for: shown with its price + reason, never scored."""

    slug: str
    question: str
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
    price_source: str  # "manifold-live" or "curation-snapshot"
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


def _catalog_as_of(catalog: MarketCatalog) -> date:
    """Model anchor date for month indexing, from catalog metadata."""
    for key in ("augur_model_as_of", "as_of"):
        if (raw := catalog.metadata.get(key)) is not None:
            return date.fromisoformat(str(raw))
    raise ValueError("catalog metadata must carry 'augur_model_as_of' or 'as_of' to anchor month indices")


def _clean_row(market: MarketSpec, trajectories: list[RolloutTrajectory], p_market: float) -> CleanRow:
    # Both guaranteed present for exact markets by MarketSpec validation; assert for type narrowing.
    assert market.mapping_kind is not None
    assert market.mapping_params is not None
    counts = Counter(
        resolve_market(t, mapping_kind=market.mapping_kind, params=dict(market.mapping_params)) for t in trajectories
    )
    yes, no, unresolved = counts[Resolution.YES], counts[Resolution.NO], counts[Resolution.UNRESOLVED]
    n = yes + no
    p_model = yes / n if n else None
    return CleanRow(
        slug=market.slug,
        mapping_kind=market.mapping_kind,
        resolution_deadline=market.resolution_deadline,
        p_market=p_market,
        p_model=p_model,
        ci95=wilson_interval(yes, n),
        n_resolved=n,
        unresolved=unresolved,
        abs_gap=abs(p_model - p_market) if p_model is not None else None,
    )


def _augur_context(market: MarketSpec, trajectories: list[RolloutTrajectory]) -> AugurContext | None:
    """The nearest clean augur signal for a surfaced market, where one exists.

    Currently only the IPO-timing correlate: P(PUBLIC_MARKET_OPEN by the deadline)
    for markets whose `correlate_of` is `ipo_by_date`.
    """
    if market.correlate_of != "ipo_by_date" or market.resolution_deadline is None or not trajectories:
        return None
    by_month = trajectories[0].month_on_or_before(market.resolution_deadline)
    counts = Counter(resolve_ipo_by_date(t, by_month=by_month) for t in trajectories)
    n = counts[Resolution.YES] + counts[Resolution.NO]
    return AugurContext(
        signal="P(PUBLIC_MARKET_OPEN by deadline)",
        p_model=counts[Resolution.YES] / n if n else None,
        note="related, NOT equal to this market",
    )


def _surfaced_row(market: MarketSpec, trajectories: list[RolloutTrajectory], p_market: float) -> SurfacedRow:
    return SurfacedRow(
        slug=market.slug,
        question=market.question,
        mappability=market.mappability,
        correlate_of=market.correlate_of,
        p_market=p_market,
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
    live: bool = False,
    bundle: PrivateEquityBundle | None = None,
) -> CalibrationResult:
    """Score an exogenous model's rollouts against a curated prediction-market catalog.

    Samples `model` for `issuer` over `horizon_months`, resolves every `exact`
    market apples-to-apples, and surfaces the rest. Market price is the catalog
    curation snapshot, or current Manifold prices when `live` is set.

    Pass a pre-sampled `bundle` (from `sample_private_equity_bundle` with the same
    issuer/horizon/seeds) to reuse one rollout for both scoring and a `mark_fan`; when
    omitted, `model` is sampled here.
    """
    as_of = _catalog_as_of(catalog)
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

    live_prices = fetch_yes_probabilities([market.manifold_id for market in catalog.markets]) if live else {}

    def price(market: MarketSpec) -> float:
        return live_prices.get(market.manifold_id, market.curation_snapshot.yes_prob)

    return CalibrationResult(
        issuer=issuer,
        as_of=as_of,
        horizon_months=horizon_months,
        rollout_count=rollout_count,
        price_source="manifold-live" if live else "curation-snapshot",
        clean=[_clean_row(market, trajectories, price(market)) for market in catalog.exact_markets()],
        surfaced=[_surfaced_row(market, trajectories, price(market)) for market in catalog.surfaced_markets()],
    )
