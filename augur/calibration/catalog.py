"""Typed catalog of prediction markets to calibrate an augur model against.

A catalog is a hand-curated YAML file: top-level ``metadata`` plus a list of
``markets``. Each market is an algebraic variant keyed on ``mappability`` -- the
shape carries exactly the fields that variant needs, so invalid combinations
(e.g. a ``mapping_kind`` on an unmappable market) are unrepresentable:

  * ``exact``      (:class:`ExactMarket`)      -- a resolver scores it
    apples-to-apples from per-rollout output (carries ``mapping_kind`` +
    ``mapping_params``).
  * ``correlate``  (:class:`CorrelateMarket`)  -- augur lacks the exact concept
    but has a RELATED signal worth surfacing next to the market price (carries
    ``correlate_of`` + ``correlate_strength``).
  * ``unmappable`` (:class:`UnmappableMarket`) -- no meaningfully related augur
    signal (carries ``reason``).

The verbatim ``resolution_criterion_text`` is the source of truth a resolver
implements -- NOT the question title. This module only parses + validates the
catalog; resolution lives in ``resolvers.py`` and scoring in ``calibration.py``.
Consumers dispatch on the variant via ``isinstance`` (mypy narrows), never on the
``mappability`` string.

``p_market`` is NOT stored here: the catalog is pure market metadata and live
Manifold prices are fetched at scoring time (see ``manifold.py``).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Mappability(StrEnum):
    EXACT = "exact"
    CORRELATE = "correlate"
    UNMAPPABLE = "unmappable"


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

    @property
    def model_anchor_date(self) -> date:
        """Date month indices for `resolution_deadline`s are measured from."""
        return self.augur_model_as_of or self.as_of


class _MarketBase(BaseModel):
    """Market metadata shared by every mappability variant."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    manifold_id: str
    question: str
    outcome_type: str
    close_date: date | None = None
    # Date the YES condition must occur by; often differs from `close_date`.
    resolution_deadline: date | None = None
    sim_signal: str | None = None
    sim_fidelity: str | None = None
    # The market's verbatim resolution criteria -- the source of truth a resolver implements.
    resolution_criterion_text: str | None = None
    notes: str | None = None


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


class MarketCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: CatalogMetadata
    markets: list[MarketSpec]

    @classmethod
    def from_yaml(cls, path: Path) -> MarketCatalog:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def exact_markets(self) -> list[ExactMarket]:
        """Markets scored apples-to-apples by a resolver."""
        return [market for market in self.markets if isinstance(market, ExactMarket)]

    def surfaced_markets(self) -> list[SurfacedMarket]:
        """Markets shown with their price + reason but never scored (correlate / unmappable)."""
        return [market for market in self.markets if not isinstance(market, ExactMarket)]
