"""Resolve a catalog's macro anchors (and pre-anchor CPI history) from vendored evidence.

A catalog's `metadata.anchors` / `metadata.inflation_history` are OPTIONAL overrides. When
absent, the live spot each macro level series is anchored to — and the pre-anchor CPI history
that lets near-term `inflation_yoy` markets score — is derived from the same vendored source
data augur fits against (`augur/data/`, via `load_absolute_monthly_levels`). That keeps a
single source of truth: refreshing the vendored series moves both the model fit and the
calibration anchors together, instead of someone hand-editing index levels into catalog YAML.

The anchor for a series is its last observation on or before `model_anchor_date`. Because CPI
publishes with a lag, that observation may be a month or two before the anchor date; the CPI
history is the months immediately preceding the same anchor observation, so anchor and history
stay contiguous on the real scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from finance.augur.calibration.catalog import MarketCatalog
from finance.augur.fit.evidence_data import MonthlyLevel, load_absolute_monthly_levels

DEFAULT_INFLATION_HISTORY_MONTHS = 12


@dataclass(frozen=True)
class ResolvedAnchors:
    anchors: dict[str, float]
    inflation_history: list[float]


def resolve_anchors(
    catalog: MarketCatalog, *, history_months: int = DEFAULT_INFLATION_HISTORY_MONTHS
) -> ResolvedAnchors:
    """Anchors + inflation history for the catalog, catalog literals overriding derived values."""
    wanted = catalog.referenced_level_series()
    explicit = catalog.metadata.anchors
    anchor_month = catalog.metadata.model_anchor_date.replace(day=1)

    need_history = "inflation" in wanted and not catalog.metadata.inflation_history
    to_derive = {wire for wire in wanted if wire not in explicit}
    if need_history:
        to_derive.add("inflation")
    series_by_wire = load_absolute_monthly_levels(to_derive) if to_derive else {}

    anchors = dict(explicit)
    for wire in wanted:
        if wire not in anchors:
            anchors[wire] = _level_on_or_before(series_by_wire[wire], anchor_month, wire=wire)

    if catalog.metadata.inflation_history:
        inflation_history = list(catalog.metadata.inflation_history)
    elif need_history:
        inflation_history = _trailing_levels(
            series_by_wire["inflation"], anchor_month, history_months, wire="inflation"
        )
    else:
        inflation_history = []
    return ResolvedAnchors(anchors=anchors, inflation_history=inflation_history)


def _observations_on_or_before(series: list[MonthlyLevel], anchor_month: date) -> list[MonthlyLevel]:
    return [obs for obs in series if obs.month <= anchor_month]


def _level_on_or_before(series: list[MonthlyLevel], anchor_month: date, *, wire: str) -> float:
    observations = _observations_on_or_before(series, anchor_month)
    if not observations:
        raise ValueError(f"vendored series {wire!r} has no observation on or before {anchor_month}")
    return observations[-1].value


def _trailing_levels(series: list[MonthlyLevel], anchor_month: date, history_months: int, *, wire: str) -> list[float]:
    observations = _observations_on_or_before(series, anchor_month)
    history = observations[-(history_months + 1) : -1]
    if len(history) < history_months:
        raise ValueError(
            f"vendored series {wire!r} has only {len(history)} month(s) before its anchor observation; "
            f"need {history_months} for the inflation history"
        )
    return [obs.value for obs in history]
