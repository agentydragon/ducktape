"""The default real-network price-client wiring shared by every server entrypoint.

Every concrete deployment (`api.server`, `dev_server`, `calibration_report`) needs
the same `{Platform: PriceClient}` mapping, so the wiring lives here instead of
being duplicated. Tests still construct their own hermetic clients directly.
"""

from __future__ import annotations

from augur.calibration.kalshi import KalshiClient
from augur.calibration.manifold import ManifoldClient
from augur.calibration.platform import Platform, PriceClient
from augur.calibration.polymarket import PolymarketClient

# These clients are constructed once per server process and reused for every calibration run, so
# the TTL bounds how stale a surfaced price/title can be — not how often a single request re-fetches.
# 12h keeps the catalog cheap to re-score and, crucially, rides over the upstream APIs' intermittent
# 5xx (Kalshi in particular flaps `503 Service Unavailable` per-ticker): once a market is fetched
# successfully it stays cached for the window instead of dropping out of the next run's results.
_DEFAULT_CACHE_TTL_SECONDS = 12 * 60 * 60


def build_default_price_clients() -> dict[Platform, PriceClient]:
    return {
        Platform.MANIFOLD: ManifoldClient(cache_ttl_seconds=_DEFAULT_CACHE_TTL_SECONDS),
        Platform.POLYMARKET: PolymarketClient(cache_ttl_seconds=_DEFAULT_CACHE_TTL_SECONDS),
        Platform.KALSHI: KalshiClient(cache_ttl_seconds=_DEFAULT_CACHE_TTL_SECONDS),
    }
