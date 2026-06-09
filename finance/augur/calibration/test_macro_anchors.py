"""Tests for deriving calibration anchors from the evidence loader (with catalog overrides)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest_bazel

from finance.augur.calibration.catalog import (
    ExactMarket,
    InflationYoyMapping,
    LevelAtDateMapping,
    ManifoldRef,
    MarketCatalog,
)
from finance.augur.calibration.macro_anchors import resolve_anchors
from finance.augur.calibration.platform import Direction
from finance.augur.fit.evidence_data import load_absolute_monthly_levels

ANCHOR_DATE = date(2026, 5, 27)


def _catalog(*, anchors: dict[str, float] | None = None, inflation_history: list[float] | None = None) -> MarketCatalog:
    """A catalog referencing the sp500 and inflation level series via two exact markets."""
    metadata: dict[str, object] = {"as_of": ANCHOR_DATE.isoformat()}
    if anchors is not None:
        metadata["anchors"] = anchors
    if inflation_history is not None:
        metadata["inflation_history"] = inflation_history
    return MarketCatalog(
        metadata=metadata,
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="SPX"),
                mapping=LevelAtDateMapping(
                    series="sp500", threshold=7500.0, direction=Direction.ABOVE, at_date=date(2026, 12, 31)
                ),
            ),
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="CPI"),
                mapping=InflationYoyMapping(
                    series="inflation", threshold=0.03, direction=Direction.ABOVE, at_date=date(2026, 7, 31)
                ),
            ),
        ],
    )


def test_derives_anchor_and_history_from_evidence(synthetic_evidence_dir: Path) -> None:
    resolved = resolve_anchors(_catalog(), history_months=12)

    # Cross-check the selection logic against the loader directly: the anchor is the last
    # observation on or before the anchor month, and the history is the 12 months immediately
    # preceding that same observation (oldest first) — not hardcoded index values.
    anchor_month = ANCHOR_DATE.replace(day=1)
    levels = load_absolute_monthly_levels({"sp500", "inflation"})
    for wire in ("sp500", "inflation"):
        on_or_before = [obs for obs in levels[wire] if obs.month <= anchor_month]
        assert resolved.anchors[wire] == on_or_before[-1].value
    cpi = [obs for obs in levels["inflation"] if obs.month <= anchor_month]
    assert resolved.inflation_history == [obs.value for obs in cpi[-13:-1]]
    assert len(resolved.inflation_history) == 12


def test_explicit_anchor_overrides_derived_value(synthetic_evidence_dir: Path) -> None:
    # An explicit per-series anchor wins; the unspecified series still derives from evidence.
    resolved = resolve_anchors(_catalog(anchors={"sp500": 1234.5}))
    assert resolved.anchors["sp500"] == 1234.5
    anchor_month = ANCHOR_DATE.replace(day=1)
    cpi = [obs for obs in load_absolute_monthly_levels({"inflation"})["inflation"] if obs.month <= anchor_month]
    assert resolved.anchors["inflation"] == cpi[-1].value


def test_explicit_history_overrides_derived_history(synthetic_evidence_dir: Path) -> None:
    pinned = [100.0, 101.0, 102.0]
    resolved = resolve_anchors(_catalog(inflation_history=pinned))
    assert resolved.inflation_history == pinned


if __name__ == "__main__":
    pytest_bazel.main()
