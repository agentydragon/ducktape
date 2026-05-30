"""Live Manifold prices: the current YES probability for a binary market.

Read-only Manifold v0 market endpoint (no key required; ``MANIFOLD_API_KEY`` is
honored for rate limits). Calibration prices ALWAYS come from live Manifold, so a
fetch that fails or returns no probability raises -- there is no catalog-stored
fallback to fall through to.

``run_calibration`` takes a :class:`PriceClient` (defaulting to a real
:class:`ManifoldClient`); tests inject a stub implementing the same single-market
``fetch_yes_probability`` so they stay hermetic.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from augur.api.casing import decamelize_json

_MARKET_ENDPOINT = "https://api.manifold.markets/v0/market/"
_USER_AGENT = "augur-pm-calibration/1.0"


class PriceClient(Protocol):
    """Source of a binary market's current YES probability, keyed by Manifold market id."""

    def fetch_yes_probability(self, market_id: str) -> float: ...


class ManifoldMarket(BaseModel):
    """The subset of the Manifold v0 market payload calibration needs (snake-cased)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    probability: float | None = None


def _headers() -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if key := os.environ.get("MANIFOLD_API_KEY"):
        headers["Authorization"] = f"Key {key}"
    return headers


class ManifoldClient:
    """Live YES-probability lookups against Manifold over a shared ``httpx.Client``."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._client = httpx.Client(headers=_headers(), timeout=timeout)

    def fetch_yes_probability(self, market_id: str) -> float:
        """Current YES probability for one binary market; raises if it carries none."""
        response = self._client.get(f"{_MARKET_ENDPOINT}{market_id}")
        response.raise_for_status()
        market = ManifoldMarket.model_validate(decamelize_json(response.json()))
        if market.probability is None:
            raise ValueError(f"Manifold market {market_id!r} returned no YES probability")
        return market.probability

    def close(self) -> None:
        self._client.close()
