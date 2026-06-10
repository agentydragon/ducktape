"""Prediction-market mirror spec: platforms, roster entries, and the evidence-repo layout.

The scraper (`finance/scraper`) mirrors each rostered market into the evidence repo
under `markets/<platform>/<market_id>/`, storing the data in the form the platform
returns it (see `finance.scraper.market_mirror`). This module is the shared
vocabulary: which platforms exist, what a roster entry is, and where a market's files
live in a checkout. Read-side consumers (augur calibration, loom) and the write-side
scraper both resolve paths through the helpers here.

The roster itself is deployment configuration, not code: the scraper reads it from
YAML roster files (`--roster`, mounted from a ConfigMap in the CronJob deployment) and
unions in the markets referenced by calibration catalogs (`--catalog`). See
`example_market_roster.yaml` for the file format.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Platform(StrEnum):
    MANIFOLD = "manifold"
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


def default_deep(platform: Platform) -> bool:
    """Whether roster entries on `platform` capture full history by default.

    Manifold is the only platform with deep capture (bets + comments) so far; the
    others are snapshot-only until the harvest pipeline needs more.
    """
    return platform is Platform.MANIFOLD


class MarketEntry(BaseModel):
    """One market the scraper mirrors. `deep` adds bets + comments capture (Manifold only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: Platform
    market_id: str
    deep: bool = False
    note: str | None = Field(default=None, description="Free-text provenance for roster readers.")

    @model_validator(mode="after")
    def _validate(self) -> MarketEntry:
        # The id becomes a directory name in the evidence repo; reject anything that
        # could escape or hide the directory.
        if not self.market_id or "/" in self.market_id or self.market_id.startswith("."):
            raise ValueError(f"invalid {self.market_id=}")
        if self.deep and self.platform is not Platform.MANIFOLD:
            raise ValueError(f"deep capture is Manifold-only, got {self.platform=}")
        return self

    @property
    def provenance_label(self) -> str:
        """Stable logical id (e.g. `manifold:a3k1Rg...`), keyed in the scrape manifest."""
        return f"{self.platform}:{self.market_id}"


class MarketRoster(BaseModel):
    """Schema of a roster YAML file (deployed as a ConfigMap, mounted into the scraper)."""

    model_config = ConfigDict(extra="forbid")

    markets: list[MarketEntry]


def load_roster(path: Path) -> tuple[MarketEntry, ...]:
    return tuple(MarketRoster.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).markets)


MARKETS_SUBDIR = "markets"


def market_dir(evidence_dir: Path, platform: Platform, market_id: str) -> Path:
    return evidence_dir / MARKETS_SUBDIR / platform / market_id


def market_json_path(evidence_dir: Path, platform: Platform, market_id: str) -> Path:
    """The market's current state as the platform serves it, overwritten each sync."""
    return market_dir(evidence_dir, platform, market_id) / "market.json"


def bets_jsonl_path(evidence_dir: Path, platform: Platform, market_id: str) -> Path:
    """Full bet history, ascending createdTime, append-only (deep entries only)."""
    return market_dir(evidence_dir, platform, market_id) / "bets.jsonl"


def comments_jsonl_path(evidence_dir: Path, platform: Platform, market_id: str) -> Path:
    """All comments, ascending (createdTime, id), rewritten each sync (deep entries only)."""
    return market_dir(evidence_dir, platform, market_id) / "comments.jsonl"


def merged_roster(
    entries: Iterable[MarketEntry], catalog_refs: Iterable[tuple[Platform, str]] = ()
) -> tuple[MarketEntry, ...]:
    """Union of roster entries and catalog-referenced markets, deduped.

    Catalog refs become entries with the platform's default capture depth. On a
    collision the entry stays deep if either side is deep (deep wins), and the roster
    entry's note is preserved. Sorted for a deterministic sync order.
    """
    by_key: dict[tuple[Platform, str], MarketEntry] = {}
    for entry in entries:
        existing = by_key.get((entry.platform, entry.market_id))
        if existing is None:
            by_key[(entry.platform, entry.market_id)] = entry
        elif entry.deep and not existing.deep:
            by_key[(entry.platform, entry.market_id)] = existing.model_copy(update={"deep": True})
    for platform, market_id in catalog_refs:
        existing = by_key.get((platform, market_id))
        if existing is None:
            by_key[(platform, market_id)] = MarketEntry(
                platform=platform, market_id=market_id, deep=default_deep(platform)
            )
        elif default_deep(platform) and not existing.deep:
            by_key[(platform, market_id)] = existing.model_copy(update={"deep": True})
    return tuple(sorted(by_key.values(), key=lambda entry: (entry.platform, entry.market_id)))
