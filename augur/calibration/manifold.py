"""Best-effort Manifold client: current YES probabilities for binary markets.

Read-only Manifold v0 market endpoint (no key required; ``MANIFOLD_API_KEY`` is
honored for rate limits). Best-effort: a market that fails to fetch or carries no
probability is skipped with a warning, never raised -- live prices are an optional
overlay on the catalog's curation snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from pydantic import BaseModel, ConfigDict

from augur.api.casing import decamelize_json

logger = logging.getLogger(__name__)

_MARKET_ENDPOINT = "https://api.manifold.markets/v0/market/"
_USER_AGENT = "augur-pm-calibration/1.0"


class ManifoldMarket(BaseModel):
    """The subset of the Manifold v0 market payload calibration needs (snake-cased)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    probability: float | None = None


def _request_headers() -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if key := os.environ.get("MANIFOLD_API_KEY"):
        headers["Authorization"] = f"Key {key}"
    return headers


def fetch_yes_probabilities(market_ids: list[str], *, timeout: float = 30.0) -> dict[str, float]:
    """Current YES probabilities keyed by Manifold market id; failures skipped + warned."""
    headers = _request_headers()
    probabilities: dict[str, float] = {}
    for market_id in market_ids:
        url = f"{_MARKET_ENDPOINT}{urllib.parse.quote(market_id)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            logger.warning("Manifold live fetch failed for %s: %s", market_id, error)
            continue
        market = ManifoldMarket.model_validate(decamelize_json(payload))
        if market.probability is None:
            logger.warning("Manifold market %s returned no probability", market_id)
            continue
        probabilities[market_id] = market.probability
    return probabilities
