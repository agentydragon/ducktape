"""Tests for deriving calibration anchors from the evidence loader (with catalog overrides)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.calibration import macro_anchors
from finance.augur.calibration.catalog import (
    CatalogMetadata,
    ExactMarket,
    InflationYoyMapping,
    LevelAtDateMapping,
    ManifoldRef,
    MarketCatalog,
)
from finance.augur.calibration.macro_anchors import resolve_anchors
from finance.augur.calibration.platform import Direction
from finance.augur.fit.evidence_data import load_absolute_monthly_levels
from finance.augur.model.series import SP500_KEY, InflationKey
from finance.evidence import sources
from finance.evidence.loading import MonthlyLevel

ANCHOR_DATE = date(2026, 5, 27)


def _catalog(
    *, anchors: dict[str, float] | None = None, inflation_history: dict[date, float] | None = None
) -> MarketCatalog:
    """A catalog referencing the sp500 and inflation level series via two exact markets."""
    return MarketCatalog(
        metadata=CatalogMetadata(
            as_of=ANCHOR_DATE,
            anchors=anchors if anchors is not None else {},
            inflation_history=inflation_history if inflation_history is not None else {},
        ),
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="SPX"),
                mapping=LevelAtDateMapping(
                    series="security:SPY", threshold=7500.0, direction=Direction.ABOVE, at_date=date(2026, 12, 31)
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


def test_derives_anchor_and_dated_history_from_evidence(synthetic_evidence_dir: Path) -> None:
    resolved = resolve_anchors(_catalog())

    # Cross-check against the loader: anchors use the latest observation at/before the model
    # month, while every earlier CPI value keeps its source month (not its row position).
    anchor_month = ANCHOR_DATE.replace(day=1)
    levels = load_absolute_monthly_levels({SP500_KEY, InflationKey()})
    for key in (SP500_KEY, InflationKey()):
        on_or_before = [obs for obs in levels[key] if obs.month <= anchor_month]
        assert resolved.anchors[key.wire_id] == on_or_before[-1].value
    cpi = levels[InflationKey()]
    assert resolved.inflation_history == {obs.month: obs.value for obs in cpi if obs.month < anchor_month}


def test_dated_history_keeps_publication_lag_and_internal_gap(
    synthetic_evidence_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = load_absolute_monthly_levels({SP500_KEY, InflationKey()})
    lagged_cpi = [
        obs for obs in loaded[InflationKey()] if obs.month <= date(2026, 3, 1) and obs.month != date(2025, 7, 1)
    ]
    monkeypatch.setattr(
        macro_anchors,
        "load_absolute_monthly_levels",
        lambda keys: {key: lagged_cpi if key == InflationKey() else loaded[key] for key in keys},
    )

    resolved = resolve_anchors(_catalog())

    assert resolved.anchors["inflation"] == next(obs.value for obs in lagged_cpi if obs.month == date(2026, 3, 1))
    assert date(2026, 3, 1) in resolved.inflation_history  # last observation is two months before model as_of
    assert date(2026, 4, 1) not in resolved.inflation_history
    assert date(2025, 6, 1) in resolved.inflation_history
    assert date(2025, 7, 1) not in resolved.inflation_history
    assert date(2025, 8, 1) in resolved.inflation_history


def test_explicit_anchor_overrides_derived_value(synthetic_evidence_dir: Path) -> None:
    # An explicit per-series anchor wins; the unspecified series still derives from evidence.
    resolved = resolve_anchors(_catalog(anchors={"security:SPY": 1234.5}))
    assert resolved.anchors["security:SPY"] == 1234.5
    anchor_month = ANCHOR_DATE.replace(day=1)
    cpi = [obs for obs in load_absolute_monthly_levels({InflationKey()})[InflationKey()] if obs.month <= anchor_month]
    assert resolved.anchors["inflation"] == cpi[-1].value


def test_explicit_history_overrides_derived_history(synthetic_evidence_dir: Path) -> None:
    pinned = {date(2025, 7, 1): 100.0, date(2025, 8, 1): 101.0, date(2026, 3, 1): 102.0}
    resolved = resolve_anchors(_catalog(anchors={"security:SPY": 1.0, "inflation": 200.0}, inflation_history=pinned))
    assert resolved.inflation_history == pinned
    assert resolved.anchors["inflation"] == 200.0


def test_inflation_history_schema_requires_pre_anchor_month_starts() -> None:
    with pytest.raises(ValueError, match="first-of-month dates before model_anchor_date"):
        CatalogMetadata(as_of=ANCHOR_DATE, inflation_history={date(2025, 7, 15): 100.0})
    with pytest.raises(ValueError, match="first-of-month dates before model_anchor_date"):
        CatalogMetadata(as_of=ANCHOR_DATE, inflation_history={date(2026, 5, 1): 100.0})
    with pytest.raises(ValueError, match="Input should be a valid dictionary"):
        CatalogMetadata.model_validate({"as_of": ANCHOR_DATE, "inflation_history": [100.0, 101.0]})


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_explicit_inflation_history_requires_positive_finite_levels(value: float) -> None:
    with pytest.raises(ValueError, match="validation error"):
        CatalogMetadata(as_of=ANCHOR_DATE, inflation_history={date(2025, 7, 1): value})


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_derived_inflation_history_rejects_invalid_levels(value: float) -> None:
    with pytest.raises(ValueError, match="invalid level"):
        macro_anchors._dated_history([MonthlyLevel(month=date(2025, 7, 1), value=value)], date(2026, 5, 1))


@pytest.mark.parametrize("invalid_index", ["0", "-1"])
def test_derived_price_anchor_rejects_nonpositive_fred_index(synthetic_evidence_dir: Path, invalid_index: str) -> None:
    path = synthetic_evidence_dir / sources.FRED_SP500.output_filename
    rows = path.read_text().splitlines()
    rows[-1] = f"{rows[-1].split(',')[0]},{invalid_index}"
    path.write_text("\n".join(rows) + "\n")

    with pytest.raises(ValueError, match="invalid level"):
        resolve_anchors(_catalog())


@pytest.mark.parametrize("invalid_index", ["0", "-1"])
def test_derived_cpi_history_rejects_nonpositive_levels(synthetic_evidence_dir: Path, invalid_index: str) -> None:
    path = synthetic_evidence_dir / sources.FRED_CPI.output_filename
    rows = path.read_text().splitlines()
    for index, row in enumerate(rows):
        if row.startswith("2025-07-"):
            rows[index] = f"{row.split(',')[0]},{invalid_index}"
            break
    else:
        raise AssertionError("fixture must contain July 2025 CPI")
    path.write_text("\n".join(rows) + "\n")

    with pytest.raises(ValueError, match="invalid level"):
        resolve_anchors(_catalog())


if __name__ == "__main__":
    pytest_bazel.main()
