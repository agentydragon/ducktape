"""Pytest fixtures for simulator scenarios."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

import pytest

from augur.model.deterministic import Deterministic
from augur.model.level_series_groups import AssetPriceGroups
from augur.model.series import CryptoSymbol
from augur.model.series_model import SeriesModelBundle
from augur.model.sim_backend import SimBackend, use_backend
from augur.sim.locations import Location

DeterministicSeriesModelBundleFactory = Callable[[Sequence[float]], SeriesModelBundle]

# Module-level singleton so the fixture's default isn't a call in arg defaults (ruff B008).
_DEFAULT_SYMBOL = CryptoSymbol("vti")


@pytest.fixture(params=list(SimBackend), ids=lambda backend: backend.value, autouse=True)
def backend(request: pytest.FixtureRequest) -> Iterator[SimBackend]:
    """Run every simulator test against both backends (NumPy reference + the in-progress JAX port).

    Autouse so the existing suite doubles as the parity harness with no per-test edits: each test
    runs once per backend under the same assertions. JAX variants for scenarios that exercise
    not-yet-ported phases are expected to fail until the port lands those phases.
    """
    with use_backend(request.param):
        yield request.param


@pytest.fixture
def deterministic_series_bundle() -> DeterministicSeriesModelBundleFactory:
    def build(levels: Sequence[float], *, symbol: CryptoSymbol = _DEFAULT_SYMBOL) -> SeriesModelBundle:
        # The fixture's series lives in the asset-price magisterium (a crypto symbol); all
        # callers take the default. No flat LevelSeriesKey map is constructed.
        return SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(crypto={symbol: Deterministic(levels=list(levels))})
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
