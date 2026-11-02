"""Resolve a catalog's macro anchors and dated CPI history from the evidence loader.

A catalog's `metadata.anchors` / `metadata.inflation_history` are OPTIONAL overrides. When
absent, the live spot each macro level series is anchored to — and the pre-anchor CPI history
that lets near-term `inflation_yoy` markets score — is derived from the same source data augur
fits against (via `load_absolute_monthly_levels`). That keeps a single source of truth:
refreshing the evidence moves both the model fit and the calibration anchors together, instead
of someone hand-editing index levels into catalog YAML.

The anchor for a series is its last observation on or before `model_anchor_date`. CPI history
retains each observation's calendar month so a YoY denominator can require its exact month even
when CPI publication lags the model date or the source has gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from finance.augur.calibration.catalog import MarketCatalog
from finance.augur.fit.evidence_data import load_absolute_monthly_levels
from finance.augur.model.series import parse_level_series_key
from finance.evidence.loading import MonthlyLevel


@dataclass(frozen=True)
class ResolvedAnchors:
    anchors: dict[str, float]
    inflation_history: dict[date, float]


def resolve_anchors(catalog: MarketCatalog) -> ResolvedAnchors:
    """Anchors + inflation history for the catalog, catalog literals overriding derived values."""
    wanted = catalog.referenced_level_series()
    explicit = catalog.metadata.anchors
    anchor_month = catalog.metadata.model_anchor_date.replace(day=1)

    need_history = "inflation" in wanted and not catalog.metadata.inflation_history
    to_derive = {wire for wire in wanted if wire not in explicit}
    if need_history:
        to_derive.add("inflation")
    # The catalog is a config file, so it names series by wire id; `load_absolute_monthly_levels`
    # is typed. `parse_level_series_key` is the documented decode point between the two.
    series_by_key = load_absolute_monthly_levels({parse_level_series_key(wire) for wire in to_derive})
    series_by_wire = {key.wire_id: levels for key, levels in series_by_key.items()}

    anchors = dict(explicit)
    for wire in wanted:
        if wire not in anchors:
            anchors[wire] = _level_on_or_before(series_by_wire[wire], anchor_month, wire=wire)

    if catalog.metadata.inflation_history:
        inflation_history = dict(catalog.metadata.inflation_history)
    elif need_history:
        inflation_history = _dated_history(series_by_wire["inflation"], anchor_month)
    else:
        inflation_history = {}
    return ResolvedAnchors(anchors=anchors, inflation_history=inflation_history)


def _observations_on_or_before(series: list[MonthlyLevel], anchor_month: date) -> list[MonthlyLevel]:
    return [obs for obs in series if obs.month <= anchor_month]


def _level_on_or_before(series: list[MonthlyLevel], anchor_month: date, *, wire: str) -> float:
    observations = _observations_on_or_before(series, anchor_month)
    if not observations:
        raise ValueError(f"vendored series {wire!r} has no observation on or before {anchor_month}")
    return observations[-1].value


def _dated_history(series: list[MonthlyLevel], anchor_month: date) -> dict[date, float]:
    """Keep each observed CPI value under its actual month, without filling source gaps."""
    return {obs.month: obs.value for obs in series if obs.month < anchor_month}
