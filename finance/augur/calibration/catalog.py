"""Typed catalog of prediction markets to calibrate an augur model against.

A catalog is a hand-curated YAML file: top-level ``metadata`` plus a list of
``markets``. Each market identifies its platform via a :class:`PlatformRef`
discriminated union (each variant carries exactly its own required ID field) and
its mappability via an orthogonal variant (``exact`` / ``correlate`` /
``unmappable``). Invalid combinations (wrong ID for platform, missing ID) are
unrepresentable.

Each exact market's ``mapping`` (the kind + params, e.g. ``level_at_date``) is the
resolver's contract -- resolution lives in ``resolvers.py`` and scoring in
``calibration.py``. Consumers dispatch on the mappability variant via ``isinstance``
(mypy narrows), never on the ``mappability`` string.

The catalog stores NEITHER live prices NOR human-readable market text (the
``question``/title and verbatim resolution criterion): all three are fetched live at
scoring time via the platform clients (``Market.quote`` / ``.title`` / ``.rules``),
so they can never drift from the platform. The catalog is the stable mapping + provenance
(IDs, ``notes``) only.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance.augur.calibration.platform import Direction, Platform


class Mappability(StrEnum):
    EXACT = "exact"
    CORRELATE = "correlate"
    UNMAPPABLE = "unmappable"


# ---------------------------------------------------------------------------
# Platform reference: discriminated union (one required ID per variant)
# ---------------------------------------------------------------------------


class ManifoldRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal[Platform.MANIFOLD] = Platform.MANIFOLD
    manifold_id: str


class PolymarketRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal[Platform.POLYMARKET] = Platform.POLYMARKET
    polymarket_id: str


class KalshiRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal[Platform.KALSHI] = Platform.KALSHI
    kalshi_id: str


PlatformRef = Annotated[ManifoldRef | PolymarketRef | KalshiRef, Field(discriminator="platform")]


def _extract_platform_ref(data: dict[str, Any]) -> dict[str, Any]:
    """Lift flat ``{platform, manifold_id, polymarket_id, kalshi_id}`` into ``platform_ref``.

    Existing catalogs write ``manifold_id`` at the top level with no explicit
    ``platform`` field. This validator normalises both old and new shapes into
    the nested ``platform_ref`` that the typed model expects.
    """
    if "platform_ref" in data:
        return data
    platform = data.pop("platform", Platform.MANIFOLD)
    ref: dict[str, Any] = {"platform": platform}
    id_key = {Platform.MANIFOLD: "manifold_id", Platform.POLYMARKET: "polymarket_id", Platform.KALSHI: "kalshi_id"}[
        Platform(platform)
    ]
    if id_key in data:
        ref[id_key] = data.pop(id_key)
    data["platform_ref"] = ref
    return data


# ---------------------------------------------------------------------------
# Catalog metadata
# ---------------------------------------------------------------------------


class CatalogMetadata(BaseModel):
    """Provenance/documentation block at the top of a catalog YAML.

    `extra="allow"` (not the repo default `extra="forbid"`): the metadata block is a
    hand-curated, per-catalog bag of provenance and reader notes (`source`, glossary
    `mapping_kinds`, `valuation_caveat`, `field_semantics`, ...) whose shape varies by
    catalog. Only the anchor dates are load-bearing, so we type those and preserve the
    rest verbatim rather than forcing every documentation key into the schema.
    """

    model_config = ConfigDict(extra="allow")

    as_of: date
    augur_model_as_of: date | None = None
    # OPTIONAL per-series override of the live spot each macro level series is anchored to at
    # `model_anchor_date`, keyed by wire id ("sp500", "inflation"). Macro markets are scored
    # against the sampled path ANCHORED to this spot — a threshold like "S&P >= 7500" is
    # meaningless unless month 0 of the path is today's real index level. When a referenced
    # series is absent here, `macro_anchors.resolve_anchors` derives the spot from the scraped
    # exogenous evidence (read at `AUGUR_EVIDENCE_DIR`), the single source of truth shared with the model fit.
    anchors: dict[str, float] = Field(default_factory=dict)
    # OPTIONAL override of the real CPI-U index for the months immediately BEFORE
    # `model_anchor_date`, oldest first (last entry = the month before the anchor observation;
    # the anchor value is `anchors.inflation`, same units). Lets an `inflation_yoy` market whose
    # year-ending date is within a year of as_of look back to real data instead of resolving
    # UNRESOLVED. When empty, derived from the vendored CPI series by `resolve_anchors`.
    inflation_history: list[float] = Field(default_factory=list)

    @property
    def model_anchor_date(self) -> date:
        """Date month indices for `resolution_deadline`s are measured from."""
        return self.augur_model_as_of or self.as_of


# ---------------------------------------------------------------------------
# Market base + mappability variants
# ---------------------------------------------------------------------------


class _MarketBase(BaseModel):
    """Market metadata shared by every mappability variant."""

    model_config = ConfigDict(extra="forbid")

    # The catalog stores ONLY the stable mapping + provenance. A market's human-readable text
    # (`question`/title, verbatim resolution criterion) and its platform metadata (`outcome_type`,
    # `close_date`) are NOT stored here -- they are fetched live alongside the price (Market.title /
    # Market.rules / ...) so they can never drift from the platform.
    platform_ref: PlatformRef
    # Date the YES condition must occur by (used by the IPO-correlate context). Kept because the
    # model needs the deadline as a month index; it isn't reliably derivable from the live market.
    resolution_deadline: date | None = None
    notes: str | None = None

    normalize_platform_fields = model_validator(mode="before")(_extract_platform_ref)

    @property
    def platform(self) -> Platform:
        return self.platform_ref.platform

    @property
    def market_id(self) -> str:
        ref = self.platform_ref
        if isinstance(ref, ManifoldRef):
            return ref.manifold_id
        if isinstance(ref, PolymarketRef):
            return ref.polymarket_id
        if isinstance(ref, KalshiRef):
            return ref.kalshi_id
        raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Market mapping: a discriminated union binding an `exact` market to the augur
# quantity that scores it. Each variant carries exactly its own fields (a PE event
# kind never has a `series`, a level kind never has a `threshold_usd`), so invalid
# bindings are unrepresentable. `kind` is the discriminator.
# ---------------------------------------------------------------------------


# PE event mappings name the private-equity issuer they score (the catalog self-describes its
# targets; the run covers the union of referenced issuers).
_ISSUER_PATTERN = r"^[a-z0-9][a-z0-9_\-]*$"


class IpoByDateMapping(BaseModel):
    """An IPO / public-listing (PUBLIC_MARKET_OPEN) event occurs for `issuer` by `by_date`."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["ipo_by_date"] = "ipo_by_date"
    issuer: str = Field(pattern=_ISSUER_PATTERN)
    by_date: date


class PreIpoFailureMapping(BaseModel):
    """An absorbing COLLAPSED/ACQUIRED exit for `issuer` before any PUBLIC_MARKET_OPEN."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["pre_ipo_failure"] = "pre_ipo_failure"
    issuer: str = Field(pattern=_ISSUER_PATTERN)


class ValuationByDateMapping(BaseModel):
    """`issuer`'s valuation `V(m) >= threshold_usd` for some month m <= `by_date` (opt-in M2 channel)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["valuation_by_date"] = "valuation_by_date"
    issuer: str = Field(pattern=_ISSUER_PATTERN)
    threshold_usd: float
    by_date: date


class LevelAtDateMapping(BaseModel):
    """A point-in-time threshold on a level series: `value(at_date) {direction} threshold`."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["level_at_date"] = "level_at_date"
    series: str  # level-series wire id ("sp500", "inflation")
    threshold: float
    direction: Direction
    at_date: date


class InflationYoyMapping(BaseModel):
    """Trailing year-over-year change of an index series: `yoy(at_date) {direction} threshold`."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["inflation_yoy"] = "inflation_yoy"
    series: str  # level-series wire id ("inflation")
    threshold: float  # a fraction, e.g. 0.03 for 3%
    direction: Direction
    at_date: date
    window_months: int = 12


class LevelByDateMapping(BaseModel):
    """An ever-by-date ("touch") threshold on a level series: the value reaches `direction`'s side
    of `threshold` at SOME month on/before `by_date` (e.g. "BTC reaches $150k by D", "S&P ATH by D")."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["level_by_date"] = "level_by_date"
    series: str  # level-series wire id ("sp500", "crypto:btc")
    threshold: float
    direction: Direction
    by_date: date


# PE event mappings read a per-issuer trajectory; level mappings read a level-series matrix.
PeEventMapping = IpoByDateMapping | PreIpoFailureMapping | ValuationByDateMapping
LevelMapping = LevelAtDateMapping | InflationYoyMapping | LevelByDateMapping
MarketMapping = Annotated[
    IpoByDateMapping
    | PreIpoFailureMapping
    | ValuationByDateMapping
    | LevelAtDateMapping
    | InflationYoyMapping
    | LevelByDateMapping,
    Field(discriminator="kind"),
]


class ExactMarket(_MarketBase):
    """A market augur scores apples-to-apples from per-rollout output."""

    mappability: Literal[Mappability.EXACT] = Mappability.EXACT
    mapping: MarketMapping


class CorrelateMarket(_MarketBase):
    """A market augur cannot score, but for which it has a RELATED signal to surface."""

    mappability: Literal[Mappability.CORRELATE] = Mappability.CORRELATE
    correlate_of: str
    correlate_strength: str | None = None
    reason: str | None = None
    # The PE issuer whose signal to surface (e.g. for `correlate_of: ipo_by_date`, P(IPO by
    # deadline) for this issuer). None when the correlate has no per-issuer signal.
    issuer: str | None = Field(default=None, pattern=_ISSUER_PATTERN)


class UnmappableMarket(_MarketBase):
    """A market with no meaningfully related augur signal."""

    mappability: Literal[Mappability.UNMAPPABLE] = Mappability.UNMAPPABLE
    reason: str | None = None


# Surfaced markets (shown with their live price + reason, never scored) are the non-exact variants.
SurfacedMarket = CorrelateMarket | UnmappableMarket
MarketSpec = Annotated[ExactMarket | CorrelateMarket | UnmappableMarket, Field(discriminator="mappability")]


# ---------------------------------------------------------------------------
# Categorical (multinomial) bucket families
# ---------------------------------------------------------------------------


class BucketMember(BaseModel):
    """One mutually-exclusive bucket of a categorical family: a half-open `[low, high)` interval.

    `low=None` is an open lower end (`-inf`, the platform's "below X" bucket);
    `high=None` is an open upper end (`+inf`, the "above X" bucket). `market_id`
    is the platform-native id of the bucket's own binary market (each bucket is a
    separately-priced market on the platform).
    """

    model_config = ConfigDict(extra="forbid")

    market_id: str
    label: str
    low: float | None = None
    high: float | None = None


class BucketFamily(BaseModel):
    """A family of mutually-exclusive buckets over one level series at a single date.

    Scored as a categorical: each bucket's live binary price is normalized into a
    market categorical, the model categorical is the fraction of rollouts landing
    in each bucket at `at_date`, and the family carries one multinomial
    `D_KL(market ‖ model)`. The buckets should tile the line; the engine does not
    require it but uncovered rollouts are simply uncounted.
    """

    model_config = ConfigDict(extra="forbid")

    family_id: str
    question: str
    platform: Platform
    series: str  # level-series wire id ("sp500", "inflation")
    at_date: date
    buckets: list[BucketMember] = Field(min_length=2)


class ThresholdLadderMember(BaseModel):
    """One cumulative threshold contract in a ladder family.

    Example: Kalshi CPI YoY has separate binary contracts for "Above 3.0%",
    "Above 3.1%", ... . Those contracts are not mutually exclusive buckets; they
    are survival/CDF points that must be differenced into buckets before scoring.
    """

    model_config = ConfigDict(extra="forbid")

    market_id: str
    threshold: float


class ThresholdLadderFamily(BaseModel):
    """A cumulative threshold ladder over one level-derived quantity.

    The child markets represent cumulative probabilities (`value > threshold` for
    `direction=above`, or `value < threshold` for `direction=below`). Calibration
    fits a monotone curve over those child prices, differences adjacent points into
    a one-of-N bucket distribution, then scores that distribution as one categorical
    row against the model's bucket counts.
    """

    model_config = ConfigDict(extra="forbid")

    family_id: str
    question: str
    platform: Platform
    series: str
    value_kind: Literal["level_at_date", "inflation_yoy"] = "level_at_date"
    direction: Direction = Direction.ABOVE
    at_date: date
    window_months: int = 12
    thresholds: list[ThresholdLadderMember] = Field(min_length=2)


class DateLadderMember(BaseModel):
    """One cumulative date-threshold event contract in a ladder family.

    Example: "OpenAI IPO before Sep 1", "before Oct 1", ... . These are CDF
    points over event timing, not independent exact markets; calibration
    differences adjacent fitted points into timing buckets.
    """

    model_config = ConfigDict(extra="forbid")

    market_id: str
    by_date: date


class DateLadderFamily(BaseModel):
    """A cumulative event-timing ladder over one private-equity issuer.

    The child markets represent `P(event occurs by date)` for increasing dates.
    Calibration fits a monotone CDF over those child prices, differences adjacent
    dates into one-of-N timing buckets, then scores that distribution as one
    categorical row against the issuer's rollout event timings.
    """

    model_config = ConfigDict(extra="forbid")

    family_id: str
    question: str
    platform: Platform
    kind: Literal["ipo_by_date"] = "ipo_by_date"
    issuer: str = Field(pattern=_ISSUER_PATTERN)
    dates: list[DateLadderMember] = Field(min_length=2)


class MarketCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: CatalogMetadata
    markets: list[MarketSpec]
    bucket_families: list[BucketFamily] = Field(default_factory=list)
    threshold_ladder_families: list[ThresholdLadderFamily] = Field(default_factory=list)
    date_ladder_families: list[DateLadderFamily] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> MarketCatalog:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def exact_markets(self) -> list[ExactMarket]:
        """Markets scored apples-to-apples by a resolver."""
        return [market for market in self.markets if isinstance(market, ExactMarket)]

    def surfaced_markets(self) -> list[SurfacedMarket]:
        """Markets shown with their price + reason but never scored (correlate / unmappable)."""
        return [market for market in self.markets if not isinstance(market, ExactMarket)]

    def referenced_markets(self) -> set[tuple[Platform, str]]:
        """Every `(platform, market_id)` the catalog references, deduped.

        One platform market can back several catalog rows (an exact market and a correlate of it,
        a bucket and a ladder rung), so this is the deduped set every consumer fetches exactly
        once: `run_calibration` to score the live price, the cache warmer to pre-populate the
        shared snapshot cache out of band.
        """
        refs: set[tuple[Platform, str]] = {(market.platform, market.market_id) for market in self.markets}
        refs.update((family.platform, bucket.market_id) for family in self.bucket_families for bucket in family.buckets)
        refs.update(
            (family.platform, threshold.market_id)
            for family in self.threshold_ladder_families
            for threshold in family.thresholds
        )
        refs.update(
            (family.platform, member.market_id) for family in self.date_ladder_families for member in family.dates
        )
        return refs

    def referenced_level_series(self) -> set[str]:
        """Wire ids of every level series any macro market / bucket family scores against."""
        series = {str(family.series) for family in self.bucket_families}
        series |= {str(family.series) for family in self.threshold_ladder_families}
        for market in self.exact_markets():
            if isinstance(market.mapping, LevelMapping):
                series.add(market.mapping.series)
        return series

    def referenced_issuers(self) -> set[str]:
        """Every PE issuer the catalog's exact PE markets score (and correlate signals reference)."""
        issuers = {
            market.mapping.issuer for market in self.exact_markets() if isinstance(market.mapping, PeEventMapping)
        }
        issuers |= {market.issuer for market in self.markets if isinstance(market, CorrelateMarket) and market.issuer}
        issuers |= {family.issuer for family in self.date_ladder_families}
        return issuers
