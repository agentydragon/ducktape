"""Typed catalog of prediction markets to calibrate an augur model against.

A catalog is a hand-curated YAML file: top-level ``metadata`` plus a list of
``markets``. Each market records how it relates to a modeled augur quantity via
``mappability``:

  * ``exact``     -- a resolver scores it apples-to-apples from per-rollout output
                     (needs ``mapping_kind`` + ``mapping_params``).
  * ``correlate`` -- augur lacks the exact concept but has a RELATED signal worth
                     surfacing next to the market price (needs ``correlate_of``).
  * ``unmappable`` -- no meaningfully related augur signal.

The verbatim ``resolution_criterion_text`` is the source of truth a resolver
implements -- NOT the question title. This module only parses + validates the
catalog; resolution lives in ``resolvers.py`` and scoring in ``calibration.py``.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


class Mappability(StrEnum):
    EXACT = "exact"
    CORRELATE = "correlate"
    UNMAPPABLE = "unmappable"


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CurationSnapshot(_CatalogModel):
    """Informational price/liquidity captured when the market was curated."""

    as_of: date | None = None
    yes_prob: float
    total_liquidity: float | None = None
    unique_bettors: int | None = None
    volume: float | None = None


class MarketSpec(_CatalogModel):
    slug: str
    manifold_id: str
    question: str
    outcome_type: str
    close_date: date | None = None
    # Date the YES condition must occur by; often differs from `close_date`.
    resolution_deadline: date | None = None
    mappability: Mappability
    # Present only for `exact` markets (validated below).
    mapping_kind: str | None = None
    mapping_params: dict[str, object] | None = None
    # Present only for `correlate` markets (validated below).
    correlate_of: str | None = None
    correlate_strength: str | None = None
    reason: str | None = None
    sim_signal: str | None = None
    sim_fidelity: str | None = None
    # The market's verbatim resolution criteria -- the source of truth a resolver implements.
    resolution_criterion_text: str | None = None
    curation_snapshot: CurationSnapshot
    notes: str | None = None

    @model_validator(mode="after")
    def _check_mapping(self) -> MarketSpec:
        match self.mappability:
            case Mappability.EXACT:
                if not self.mapping_kind or self.mapping_params is None:
                    raise ValueError(
                        f"market {self.slug!r}: exact mappability requires mapping_kind and mapping_params"
                    )
            case Mappability.CORRELATE:
                if not self.correlate_of:
                    raise ValueError(f"market {self.slug!r}: correlate mappability requires correlate_of")
        return self


class MarketCatalog(_CatalogModel):
    metadata: dict[str, object]
    markets: list[MarketSpec]

    @classmethod
    def from_yaml(cls, path: Path) -> MarketCatalog:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def exact_markets(self) -> list[MarketSpec]:
        """Markets scored apples-to-apples by a resolver."""
        return [market for market in self.markets if market.mappability is Mappability.EXACT]

    def surfaced_markets(self) -> list[MarketSpec]:
        """Markets shown with their price + reason but never scored (correlate / unmappable)."""
        return [market for market in self.markets if market.mappability is not Mappability.EXACT]
