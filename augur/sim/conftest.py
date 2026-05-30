"""Pytest fixtures for simulator scenarios."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from augur.model.deterministic import Deterministic
from augur.model.series import CryptoKey, CryptoSymbol, LevelSeriesKey
from augur.model.series_model import SeriesModelBundle
from augur.sim.locations import Location

DeterministicSeriesModelBundleFactory = Callable[[Sequence[float]], SeriesModelBundle]

# Module-level singleton so the fixture's default isn't a call in arg defaults (ruff B008).
_DEFAULT_SERIES_KEY: LevelSeriesKey = CryptoKey(symbol=CryptoSymbol("vti"))


@pytest.fixture
def deterministic_series_bundle() -> DeterministicSeriesModelBundleFactory:
    def build(levels: Sequence[float], *, key: LevelSeriesKey = _DEFAULT_SERIES_KEY) -> SeriesModelBundle:
        return SeriesModelBundle.independent({key: Deterministic(levels=list(levels))})

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
