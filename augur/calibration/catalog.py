"""Typed catalog of prediction markets to calibrate an augur model against.

A catalog is a hand-curated YAML file: top-level ``metadata`` plus a list of
``markets``. Each market identifies its platform via a :class:`PlatformRef`
discriminated union (each variant carries exactly its own required ID field) and
its mappability via an orthogonal variant (``exact`` / ``correlate`` /
``unmappable``). Invalid combinations (wrong ID for platform, missing ID) are
unrepresentable.

The verbatim ``resolution_criterion_text`` is the source of truth a resolver
implements -- NOT the question title. This module only parses + validates the
catalog; resolution lives in ``resolvers.py`` and scoring in ``calibration.py``.
Consumers dispatch on the mappability variant via ``isinstance`` (mypy narrows),
never on the ``mappability`` string.

``p_market`` is NOT stored here: the catalog is pure market metadata and live
prices are fetched at scoring time via platform-specific clients.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from augur.calibration.platform import Platform


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
    # Live spot value of each macro level series at `model_anchor_date`, keyed by the
    # series wire id ("sp500", "inflation"). Macro markets are scored against the
    # sampled path ANCHORED to this spot — a threshold like "S&P >= 7500" is
    # meaningless unless month 0 of the path is today's real index level. Refreshed
    # alongside the catalog (data, not model).
    anchors: dict[str, float] = Field(default_factory=dict)

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

    question: str
    platform_ref: PlatformRef
    outcome_type: str
    close_date: date | None = None
    # Date the YES condition must occur by; often differs from `close_date`.
    resolution_deadline: date | None = None
    # The market's verbatim resolution criteria -- the source of truth a resolver implements.
    resolution_criterion_text: str | None = None
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


class ExactMarket(_MarketBase):
    """A market augur scores apples-to-apples from per-rollout output."""

    mappability: Literal[Mappability.EXACT] = Mappability.EXACT
    mapping_kind: str
    mapping_params: dict[str, object]


class CorrelateMarket(_MarketBase):
    """A market augur cannot score, but for which it has a RELATED signal to surface."""

    mappability: Literal[Mappability.CORRELATE] = Mappability.CORRELATE
    correlate_of: str
    correlate_strength: str | None = None
    reason: str | None = None


class UnmappableMarket(_MarketBase):
    """A market with no meaningfully related augur signal."""

    mappability: Literal[Mappability.UNMAPPABLE] = Mappability.UNMAPPABLE
    reason: str | None = None


# Surfaced markets (shown with their live price + reason, never scored) are the non-exact variants.
SurfacedMarket = CorrelateMarket | UnmappableMarket
MarketSpec = Annotated[ExactMarket | CorrelateMarket | UnmappableMarket, Field(discriminator="mappability")]

# Macro `mapping_kind`s resolved against a level-series `(rollout, month)` matrix rather than the
# PE bundle. Their `mapping_params` carry a `series` wire id ("sp500", "inflation"). Event PE kinds
# (`ipo_by_date`, `pre_ipo_failure`, `valuation_by_date`) read the issuer trajectory instead.
LEVEL_MAPPING_KINDS = frozenset({"level_at_date", "inflation_yoy"})


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


class MarketCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: CatalogMetadata
    markets: list[MarketSpec]
    bucket_families: list[BucketFamily] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> MarketCatalog:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def exact_markets(self) -> list[ExactMarket]:
        """Markets scored apples-to-apples by a resolver."""
        return [market for market in self.markets if isinstance(market, ExactMarket)]

    def surfaced_markets(self) -> list[SurfacedMarket]:
        """Markets shown with their price + reason but never scored (correlate / unmappable)."""
        return [market for market in self.markets if not isinstance(market, ExactMarket)]

    def referenced_level_series(self) -> set[str]:
        """Wire ids of every level series any macro market / bucket family scores against."""
        series = {str(family.series) for family in self.bucket_families}
        for market in self.exact_markets():
            if market.mapping_kind in LEVEL_MAPPING_KINDS:
                series.add(str(market.mapping_params["series"]))
        return series
