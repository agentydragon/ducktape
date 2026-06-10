"""The default real-network price-client wiring shared by every server entrypoint.

Every concrete deployment (`api.server`, `dev_server`, `calibration_report`) needs
the same `{Platform: PriceClient}` mapping, so the wiring lives here instead of
being duplicated. Tests still construct their own hermetic clients directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from finance.augur.calibration.kalshi import KalshiClient
from finance.augur.calibration.manifold import ManifoldClient
from finance.augur.calibration.platform import PriceClient
from finance.augur.calibration.polymarket import PolymarketClient
from finance.augur.calibration.redis_cache import (
    market_cache_config_from_env,
    open_market_cache_store,
    wrap_price_clients_with_redis_cache,
)
from finance.evidence.markets import Platform

# These clients are constructed once per server process and reused for every calibration run, so
# the TTL bounds how stale a surfaced price/title can be — not how often a single request re-fetches.
# 12h keeps the catalog cheap to re-score and, crucially, rides over the upstream APIs' intermittent
# 5xx (Kalshi in particular flaps `503 Service Unavailable` per-ticker): once a market is fetched
# successfully it stays cached for the window instead of dropping out of the next run's results.
_DEFAULT_CACHE_TTL_SECONDS = 12 * 60 * 60


@asynccontextmanager
async def default_price_clients() -> AsyncIterator[dict[Platform, PriceClient]]:
    """One process-lifetime set of live price clients, plus the shared Valkey cache store when
    `AUGUR_MARKET_CACHE_URL` is set. Enter once per server/CLI run (server lifespan / CLI `main`);
    the upstream httpx/SDK clients and the persistent Valkey store are closed on exit.
    """
    cache_config = market_cache_config_from_env()
    # With the shared Valkey cache on, the per-client in-memory TTL is redundant (the store bounds
    # staleness across the whole process), so disable it; otherwise keep the 12h in-memory window.
    platform_cache_ttl_seconds = 0.0 if cache_config is not None else _DEFAULT_CACHE_TTL_SECONDS
    upstream = _build_upstream_price_clients(cache_ttl_seconds=platform_cache_ttl_seconds)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(_aclose_all, upstream)
        if cache_config is None:
            yield upstream
            return
        store = await stack.enter_async_context(open_market_cache_store(cache_config))
        yield wrap_price_clients_with_redis_cache(upstream, cache_config, store)


def _build_upstream_price_clients(*, cache_ttl_seconds: float) -> dict[Platform, PriceClient]:
    return {
        Platform.MANIFOLD: ManifoldClient(cache_ttl_seconds=cache_ttl_seconds),
        Platform.POLYMARKET: PolymarketClient(cache_ttl_seconds=cache_ttl_seconds),
        Platform.KALSHI: KalshiClient(cache_ttl_seconds=cache_ttl_seconds),
    }


async def _aclose_all(clients: dict[Platform, PriceClient]) -> None:
    for client in clients.values():
        await client.aclose()
