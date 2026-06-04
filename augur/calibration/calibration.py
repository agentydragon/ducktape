"""Compare a whole augur model's rollouts against prediction markets.

``run_calibration`` is a pure library function over a pre-sampled rollout: the caller
passes the sampled PE ``bundle`` (covering the catalog's referenced issuers) and the
anchored ``level_paths``; it slices per-issuer trajectories, resolves every ``exact``
catalog market apples-to-apples against its own channel (a PE issuer or a level series)
— ``p_model`` + Wilson CI + unresolved share vs the LIVE market price — surfaces the
rest, and scores ``bucket_families`` as multinomials. It returns a typed
:class:`CalibrationResult` and does NOT print -- a CLI or backend renders it.

The catalog self-describes its targets (each PE market names its issuer, each macro
market its series); a market on a channel the preset doesn't emit surfaces as
``unmodeled`` rather than failing.

``p_market`` ALWAYS comes from a live prediction-market client injected as a
``Mapping[Platform, PriceClient]`` (one client per platform). Tests inject
hermetic mock clients.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import httpx
import numpy as np
import numpy.typing as npt
from polymarket.errors import PolymarketError
from pydantic import BaseModel, Field
from statsmodels.stats.proportion import proportion_confint

from augur.calibration.catalog import (
    BucketFamily,
    CorrelateMarket,
    ExactMarket,
    InflationYoyMapping,
    IpoByDateMapping,
    LevelByDateMapping,
    LevelMapping,
    MarketCatalog,
    PeEventMapping,
    PreIpoFailureMapping,
    SurfacedMarket,
)
from augur.calibration.platform import Market, Platform, PriceClient
from augur.calibration.resolvers import (
    Resolution,
    ResolutionCounts,
    RolloutTrajectory,
    bucket_model_counts,
    inflation_yoy_counts,
    level_by_date_counts,
    level_threshold_counts,
    months_after,
    resolve_ipo_by_date,
    resolve_pre_ipo_failure,
    resolve_valuation_by_date,
    trajectories_from_bundle,
)
from augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    anchor_sampled_series_levels,
    level_keys_in_bundle,
)
from augur.model.private_equity_bundle import PrivateEquityBundle, PrivateEquityFloatChannel
from augur.model.series import IssuerId, LevelSeriesKey, parse_level_series_key

logger = logging.getLogger(__name__)

# Per-market network failures we tolerate by dropping the affected row instead of failing the
# whole calibration run: httpx for the manifold + kalshi clients, PolymarketError for the
# polymarket SDK (covers "id is invalid", rate limits, transport errors, timeouts).
_LIVE_FETCH_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, PolymarketError)


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


def kl_bits_categorical(p_market: list[float], p_model: list[float]) -> float:
    """`D_KL(market ‖ model)` in bits for two categorical (multinomial) forecasts.

    The bucket-family generalization of `kl_bits_market_vs_model`: the live per-bucket
    market prices (normalized to a categorical) are the reference, the model's per-bucket
    rollout shares the candidate. Model probabilities are clamped off 0 (an empty model
    bucket would otherwise send the divergence to +inf); the `0·log0 = 0` convention
    handles empty market buckets. Both lists must be normalized and same-length.
    """
    eps = 1e-6
    total = 0.0
    for pm, qm in zip(p_market, p_model, strict=True):
        if pm > 0.0:
            total += pm * math.log2(pm / max(eps, qm))
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

    market_id: str
    question: str
    url: str
    platform: str
    # The model channel that scored this market: a PE issuer id (for event markets) or a
    # level-series wire id ("sp500", "inflation"). `= None` keeps it optional on the wire schema.
    channel: str | None = None
    p_market: float
    p_model: float | None = None  # None when no rollout resolved YES/NO within the horizon
    # 95% Wilson CI on `p_model`; None (dropped on the wire) when nothing resolved (p_model None).
    ci95: tuple[float, float] | None = None
    n_resolved: int
    unresolved: int
    kl_bits: float | None = None
    # Platform-native total traded volume + its unit (e.g. "USD", "Ṁ" mana, "contracts").
    # Both are None when the platform's response carried no volume figure.
    volume: float | None = None
    volume_unit: str | None = None


class SurfacedRow(BaseModel):
    """A market augur lacks the concept for: shown with its price + reason, never scored."""

    market_id: str
    question: str
    url: str
    platform: str
    mappability: str
    correlate_of: str | None = None
    p_market: float
    reason: str | None = None
    augur_context: AugurContext | None = None
    # Platform-native total traded volume + its unit (see `CleanRow.volume`).
    volume: float | None = None
    volume_unit: str | None = None


class CategoricalBucket(BaseModel):
    """One bucket of a scored categorical family: its live (normalized) market share vs the model's."""

    label: str
    market_id: str
    low: float | None = None
    high: float | None = None
    p_market: float
    p_model: float | None = None  # None when the family's `at_date` is beyond the sampled horizon


class CategoricalRow(BaseModel):
    """A mutually-exclusive bucket family scored as one multinomial `D_KL(market ‖ model)`.

    `p_model` per bucket and `kl_bits` are None when the family's series isn't emitted by the
    preset (unmodeled) or its `at_date` exceeds the horizon; the live per-bucket prices still
    surface so the view shows the market even when augur can't score it.
    """

    family_id: str
    question: str
    platform: str
    channel: str
    at_date: date
    n_resolved: int  # rollouts falling in some bucket at `at_date` (0 when unscored)
    kl_bits: float | None = None
    buckets: list[CategoricalBucket]


class CalibrationResult(BaseModel):
    """Model-level calibration: every market the catalog scores, across all issuers/channels.

    No single issuer — each scored row carries its own `channel` (a PE issuer id or a level
    wire id). The mark/valuation fans (per issuer) live on the API response, not here.
    """

    as_of: date
    horizon_months: int
    rollout_count: int
    clean: list[CleanRow]
    surfaced: list[SurfacedRow]
    categorical: list[CategoricalRow] = Field(default_factory=list)


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


def _clean_row(market: ExactMarket, counts: ResolutionCounts, live: Market, *, channel: str | None) -> CleanRow:
    p_market = live.require_probability()
    p_model = counts.p_model
    return CleanRow(
        market_id=market.market_id,
        question=market.question,
        url=live.url,
        platform=market.platform,
        channel=channel,
        p_market=p_market,
        p_model=p_model,
        ci95=wilson_interval(counts.yes, counts.n_resolved) if counts.n_resolved else None,
        n_resolved=counts.n_resolved,
        unresolved=counts.unresolved,
        kl_bits=kl_bits_market_vs_model(p_market, p_model) if p_model is not None else None,
        volume=live.volume,
        volume_unit=live.volume_unit,
    )


@dataclass(frozen=True)
class _Unmodeled:
    """Sentinel: an exact market binds a channel (level series or PE issuer) this preset doesn't emit."""

    target: str


def _resolve_pe(traj: RolloutTrajectory, mapping: PeEventMapping) -> Resolution:
    """Resolve a PE-event market against one rollout's issuer trajectory."""
    if isinstance(mapping, IpoByDateMapping):
        return resolve_ipo_by_date(traj, by_month=traj.month_on_or_before(mapping.by_date))
    if isinstance(mapping, PreIpoFailureMapping):
        return resolve_pre_ipo_failure(traj)
    return resolve_valuation_by_date(
        traj, threshold_usd=mapping.threshold_usd, by_month=traj.month_on_or_before(mapping.by_date)
    )


def _exact_market_counts(
    market: ExactMarket,
    *,
    trajectories_by_issuer: Mapping[str, list[RolloutTrajectory]],
    level_paths: Mapping[LevelSeriesKey, npt.NDArray[np.float64]],
    inflation_history: npt.NDArray[np.float64] | None,
    as_of: date,
    horizon_months: int,
) -> tuple[ResolutionCounts, str] | _Unmodeled:
    """Per-rollout tally + channel tag (issuer or level wire id) for one exact market.

    PE event kinds read the market's issuer trajectories (per-rollout loop); level kinds
    read the anchored level matrix (vectorized). Returns `_Unmodeled` when the bound channel
    (the issuer's PE bundle or the level series) isn't emitted by the active preset.
    """
    mapping = market.mapping
    if not isinstance(mapping, LevelMapping):
        trajectories = trajectories_by_issuer.get(mapping.issuer)
        if trajectories is None:
            return _Unmodeled(target=mapping.issuer)
        return ResolutionCounts.from_resolutions(_resolve_pe(t, mapping) for t in trajectories), mapping.issuer
    matrix = level_paths.get(parse_level_series_key(mapping.series))
    if matrix is None:
        return _Unmodeled(target=mapping.series)
    if isinstance(mapping, InflationYoyMapping):
        counts = inflation_yoy_counts(
            matrix,
            threshold=mapping.threshold,
            direction=mapping.direction,
            at_month=months_after(as_of, mapping.at_date),
            horizon_months=horizon_months,
            window_months=mapping.window_months,
            history=inflation_history,
        )
    elif isinstance(mapping, LevelByDateMapping):
        counts = level_by_date_counts(
            matrix,
            threshold=mapping.threshold,
            direction=mapping.direction,
            by_month=months_after(as_of, mapping.by_date),
            horizon_months=horizon_months,
        )
    else:
        counts = level_threshold_counts(
            matrix,
            threshold=mapping.threshold,
            direction=mapping.direction,
            at_month=months_after(as_of, mapping.at_date),
            horizon_months=horizon_months,
        )
    return counts, mapping.series


def _categorical_row(
    family: BucketFamily,
    *,
    level_paths: Mapping[LevelSeriesKey, npt.NDArray[np.float64]],
    live_prices: list[float],
    as_of: date,
    horizon_months: int,
) -> CategoricalRow:
    """Score a bucket family as one multinomial `D_KL(market ‖ model)`.

    `live_prices` are the per-bucket YES probabilities (same order as
    `family.buckets`); they're normalized into the market categorical. The model
    categorical is the per-bucket rollout share at `at_date`; both `p_model` and
    `kl_bits` are None when the series is unmodeled or `at_date` is beyond the horizon.
    """
    # The caller (`run_calibration`) guarantees a finite total > 0 before getting here.
    total = sum(live_prices)
    p_market = [price / total for price in live_prices]
    matrix = level_paths.get(parse_level_series_key(str(family.series)))
    model_counts = (
        None
        if matrix is None
        else bucket_model_counts(
            matrix,
            lows=[b.low for b in family.buckets],
            highs=[b.high for b in family.buckets],
            at_month=months_after(as_of, family.at_date),
            horizon_months=horizon_months,
        )
    )
    n_resolved = int(model_counts.sum()) if model_counts is not None else 0
    p_model = [int(c) / n_resolved for c in model_counts] if model_counts is not None and n_resolved else None
    kl_bits = kl_bits_categorical(p_market, p_model) if p_model is not None else None
    buckets = [
        CategoricalBucket(
            label=member.label,
            market_id=member.market_id,
            low=member.low,
            high=member.high,
            p_market=p_market[i],
            p_model=p_model[i] if p_model is not None else None,
        )
        for i, member in enumerate(family.buckets)
    ]
    return CategoricalRow(
        family_id=family.family_id,
        question=family.question,
        platform=family.platform,
        channel=str(family.series),
        at_date=family.at_date,
        n_resolved=n_resolved,
        kl_bits=kl_bits,
        buckets=buckets,
    )


def build_anchored_level_paths(
    sampled: SampledExogenousBundle,
    *,
    anchors: Mapping[str, float],
    requested_wire_ids: set[str],
    rollout_count: int,
    horizon_months: int,
) -> dict[LevelSeriesKey, npt.NDArray[np.float64]]:
    """Extract the catalog's macro series as anchored `(rollout, month)` matrices.

    Only series the preset actually emits are returned (others are left out so the
    caller surfaces those markets as unmodeled). Each emitted series is rescaled so
    every rollout's month-0 value matches its resolved spot anchor (a catalog override
    or, by default, the vendored evidence via `macro_anchors.resolve_anchors`) — required
    for any threshold against a real index to be meaningful. A referenced+emitted series
    with no resolved anchor raises.
    """
    emitted = level_keys_in_bundle(sampled)
    keys = {parse_level_series_key(wire) for wire in requested_wire_ids} & emitted
    anchor_map: dict[LevelSeriesKey, float] = {}
    for key in keys:
        if key.wire_id not in anchors:
            raise ValueError(
                f"catalog scores level series {key.wire_id!r} but no anchor spot was resolved for it "
                "(catalog override or vendored evidence)"
            )
        anchor_map[key] = anchors[key.wire_id]
    anchored = anchor_sampled_series_levels(sampled, level_series_anchors=anchor_map) if anchor_map else sampled
    return {key: anchored.level_matrix(key, rollout_count=rollout_count, horizon_months=horizon_months) for key in keys}


def _augur_context(
    market: SurfacedMarket, trajectories_by_issuer: Mapping[str, list[RolloutTrajectory]]
) -> AugurContext | None:
    """The nearest clean augur signal for a surfaced market, where one exists.

    Currently only the IPO-timing correlate: P(PUBLIC_MARKET_OPEN by the deadline) for the
    correlate's `issuer`, when `correlate_of` is `ipo_by_date` and that issuer is emitted.
    """
    if (
        not isinstance(market, CorrelateMarket)
        or market.correlate_of != "ipo_by_date"
        or market.resolution_deadline is None
        or market.issuer is None
    ):
        return None
    trajectories = trajectories_by_issuer.get(market.issuer)
    if not trajectories:
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
    market: SurfacedMarket, trajectories_by_issuer: Mapping[str, list[RolloutTrajectory]], live: Market
) -> SurfacedRow:
    return SurfacedRow(
        market_id=market.market_id,
        question=market.question,
        url=live.url,
        platform=market.platform,
        mappability=market.mappability,
        correlate_of=market.correlate_of if isinstance(market, CorrelateMarket) else None,
        p_market=live.require_probability(),
        reason=" ".join(market.reason.split()) if market.reason else None,
        augur_context=_augur_context(market, trajectories_by_issuer),
        volume=live.volume,
        volume_unit=live.volume_unit,
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


def _unmodeled_row(market: ExactMarket, live: Market, target: str) -> SurfacedRow:
    """Surface an exact market whose bound channel (issuer or level series) the preset doesn't emit."""
    return SurfacedRow(
        market_id=market.market_id,
        question=market.question,
        url=live.url,
        platform=market.platform,
        mappability="unmodeled",
        p_market=live.require_probability(),
        reason=f"{target!r} is not emitted by this model preset",
        volume=live.volume,
        volume_unit=live.volume_unit,
    )


def run_calibration(
    catalog: MarketCatalog,
    *,
    horizon_months: int,
    rollout_seeds: tuple[int, ...],
    price_clients: Mapping[Platform, PriceClient],
    bundle: PrivateEquityBundle,
    level_paths: Mapping[LevelSeriesKey, npt.NDArray[np.float64]] | None = None,
    inflation_history: list[float] | None = None,
) -> CalibrationResult:
    """Score a whole-model rollout against a curated prediction-market catalog.

    The catalog self-describes its targets: each PE `exact` market names its issuer, each
    macro market its level series. `bundle` is the sampled PE bundle (covering the catalog's
    referenced issuers) and `level_paths` the anchored `(rollout, month)` level matrices (from
    `build_anchored_level_paths`); a market whose issuer/series the active preset doesn't emit
    surfaces as `unmodeled` rather than failing. Each market's `p_market` is fetched LIVE per
    market via the platform-appropriate client from `price_clients` (a real client by default,
    whose TTL cache absorbs rapid auto-refreshes; tests inject hermetic clients).

    `inflation_history` is the real CPI-U for the months before month 0, anchoring the
    denominator of near-term `inflation_yoy` markets (see `macro_anchors.resolve_anchors`).
    """
    as_of = catalog.metadata.model_anchor_date
    rollout_count = len(rollout_seeds)
    paths = dict(level_paths) if level_paths is not None else {}
    inflation_history_array = np.array(inflation_history, dtype=np.float64) if inflation_history else None
    # Per-issuer trajectory slices for every catalog-referenced issuer the bundle actually carries;
    # markets on an absent issuer surface as `unmodeled`.
    emitted_issuers = {str(issuer) for issuer in bundle.issuer_ids()}
    trajectories_by_issuer = {
        issuer: list(
            trajectories_from_bundle(
                bundle, issuer=issuer, rollout_count=rollout_count, horizon_months=horizon_months, as_of=as_of
            )
        )
        for issuer in sorted(catalog.referenced_issuers() & emitted_issuers)
    }

    def _live(market_id: str, platform: Platform) -> Market | None:
        # A single broken catalog row (bad market_id), a transient API hiccup, or a market the
        # platform priced as None (e.g. Kalshi `last_price_dollars: null`) must not 500 the whole
        # calibration endpoint -- log + drop just that row instead. Dropping None-probability
        # markets here keeps every downstream `require_probability()` call safe.
        try:
            market = price_clients[platform].get_market(market_id)
        except _LIVE_FETCH_ERRORS:
            logger.warning("dropping calibration row: %s market %r failed to fetch", platform, market_id, exc_info=True)
            return None
        if market.probability is None:
            logger.warning("dropping calibration row: %s market %r returned no YES probability", platform, market_id)
            return None
        return market

    clean: list[CleanRow] = []
    surfaced: list[SurfacedRow] = []
    for market in catalog.exact_markets():
        live = _live(market.market_id, market.platform)
        if live is None:
            continue
        outcome = _exact_market_counts(
            market,
            trajectories_by_issuer=trajectories_by_issuer,
            level_paths=paths,
            inflation_history=inflation_history_array,
            as_of=as_of,
            horizon_months=horizon_months,
        )
        if isinstance(outcome, _Unmodeled):
            surfaced.append(_unmodeled_row(market, live, outcome.target))
        else:
            counts, channel = outcome
            clean.append(_clean_row(market, counts, live, channel=channel))
    surfaced.extend(
        _surfaced_row(market, trajectories_by_issuer, live)
        for market in catalog.surfaced_markets()
        if (live := _live(market.market_id, market.platform)) is not None
    )

    categorical: list[CategoricalRow] = []
    for family in catalog.bucket_families:
        prices = [_live(member.market_id, family.platform) for member in family.buckets]
        # A bucket that failed to fetch or had no probability makes the categorical ill-defined.
        if any(live is None for live in prices):
            logger.warning("dropping categorical family %r: a bucket failed to fetch or had no price", family.family_id)
            continue
        live_prices = [live.require_probability() for live in prices if live is not None]
        total = sum(live_prices)
        # Degenerate normalizer (all-zero / non-finite prices) can't form a valid categorical.
        if not math.isfinite(total) or total <= 0.0:
            logger.warning("dropping categorical family %r: bucket prices sum to %r", family.family_id, total)
            continue
        categorical.append(
            _categorical_row(
                family, level_paths=paths, live_prices=live_prices, as_of=as_of, horizon_months=horizon_months
            )
        )

    return CalibrationResult(
        as_of=as_of,
        horizon_months=horizon_months,
        rollout_count=rollout_count,
        clean=clean,
        surfaced=surfaced,
        categorical=categorical,
    )
