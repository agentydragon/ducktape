"""Pytest fixtures for simulator scenarios, and the numeric mode they assume."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import jax
import pytest

from finance.augur.model.deterministic import Deterministic
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.sim.locations import Location

# The simulator's fixed-point accounting is int64 throughout, and jax silently NARROWS int64
# to int32 unless x64 is on — so a $1M order against BTC's satoshi scale (~1e14) wraps to a
# negative quantity rather than failing loudly. `jax_engine` sets it at import and refuses to
# run without it, but the pure policy modules (`target_allocation`, `allocation`, `cash_band`)
# do the same arithmetic without importing the engine. Setting it here means a test of one of
# those computes what production computes, instead of depending on which module happened to be
# imported first. Found by a sweep whose orders came back negative.
#
# Safe after the imports above: jax reads this when an array is first created, not at import.
jax.config.update("jax_enable_x64", True)

DeterministicSeriesModelBundleFactory = Callable[[Sequence[float]], SeriesModelBundle]

# Module-level singleton so the fixture's default isn't a call in arg defaults (ruff B008).
_DEFAULT_SYMBOL = SecuritySymbol("vti")


@pytest.fixture
def deterministic_series_bundle() -> DeterministicSeriesModelBundleFactory:
    def build(levels: Sequence[float], *, symbol: SecuritySymbol = _DEFAULT_SYMBOL) -> SeriesModelBundle:
        # The fixture's series lives in the asset-price role (keyed by symbol); all
        # callers take the default. No flat LevelSeriesKey map is constructed.
        return SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(security={symbol: Deterministic(levels=list(levels))})
        )

    return build


@pytest.fixture
def san_francisco_location() -> Location:
    return Location(
        location_id="san_francisco",
        display_name="San Francisco, CA",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.01180,
        annual_special_assessment_usd=0.0,
    )


@pytest.fixture
def vallejo_mare_island_location() -> Location:
    return Location(
        location_id="vallejo_mare_island",
        display_name="Vallejo, CA — Mare Island",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.0115,
        annual_special_assessment_usd=2300.0,
    )
