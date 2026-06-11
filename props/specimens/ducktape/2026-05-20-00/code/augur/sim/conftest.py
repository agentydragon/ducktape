"""Pytest fixtures for simulator scenarios."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from augur.model.deterministic import Deterministic
from augur.model.market import MarketBundle

DeterministicMarketBundleFactory = Callable[[Sequence[float]], MarketBundle]


@pytest.fixture
def deterministic_market_bundle() -> DeterministicMarketBundleFactory:
    def build(levels: Sequence[float], *, asset_id: str = "vti") -> MarketBundle:
        return MarketBundle.independent({asset_id: Deterministic(levels=list(levels))})

    return build
