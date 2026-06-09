"""Eagerly warm the shared prediction-market snapshot cache.

`run_calibration` fetches every catalog market lazily on each request; the shared Valkey
read-through cache (`default_clients` + `redis_cache`) then absorbs repeats for the TTL window.
This module pre-populates that cache out of band so the first calibration request after a
snapshot goes stale is a cache hit instead of paying the upstream fetch (and its flakiness —
Kalshi in particular flaps 503s). A CronJob runs `warm-market-cache --catalog <catalog.yaml>`
more often than the TTL so the cache never goes cold.

It deliberately depends only on the catalog + the price clients — never on `api.config` or any
model provider — so the warmer image stays free of the JAX / simulator / FastAPI stack the
serving image carries. The CronJob points it straight at the `prediction_markets.yaml` already
mounted in the `augur-config` ConfigMap.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from polymarket.errors import PolymarketError

from finance.augur.calibration.catalog import MarketCatalog
from finance.augur.calibration.default_clients import default_price_clients
from finance.augur.calibration.platform import Platform, PriceClient
from finance.augur.calibration.redis_cache import market_cache_config_from_env

logger = logging.getLogger(__name__)

# Per-market failures we tolerate by skipping that market instead of aborting the whole warm pass:
# httpx for the manifold + kalshi clients, PolymarketError for the polymarket SDK. Mirrors
# `calibration._LIVE_FETCH_ERRORS` (a bad id / flapping upstream is expected, not a warmer bug).
_WARM_FETCH_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, PolymarketError)

# Match `calibration._MAX_CONCURRENT_MARKET_FETCHES`: cap concurrent upstream connections so a
# large catalog on a cold cache can't open hundreds at once (and trip rate limits).
_WARM_CONCURRENCY = 16


@dataclass(frozen=True)
class WarmSummary:
    requested: int
    succeeded: int
    failed: int


async def warm_market_cache(
    catalog: MarketCatalog, price_clients: Mapping[Platform, PriceClient], *, concurrency: int = _WARM_CONCURRENCY
) -> WarmSummary:
    """Fetch every catalog market through `price_clients`, populating the read-through cache.

    Each `get_market` call writes a fresh snapshot to the shared store as a side effect. Per-market
    fetch failures are logged and counted, never raised — one broken id or a single flapping upstream
    must not abort the pass — so the returned counts let the caller log + pick an exit status.
    """
    refs = sorted(catalog.referenced_markets())
    slots = asyncio.Semaphore(concurrency)

    async def _warm_one(platform: Platform, market_id: str) -> bool:
        async with slots:
            try:
                await price_clients[platform].get_market(market_id)
            except _WARM_FETCH_ERRORS:
                logger.warning("failed to warm %s market %r", platform.value, market_id, exc_info=True)
                return False
        return True

    results = await asyncio.gather(*(_warm_one(platform, market_id) for platform, market_id in refs))
    succeeded = sum(results)
    return WarmSummary(requested=len(refs), succeeded=succeeded, failed=len(refs) - succeeded)


async def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description="Eagerly warm the prediction-market snapshot cache.")
    parser.add_argument("--catalog", type=Path, required=True, help="Path to the prediction-market catalog YAML.")
    args = parser.parse_args(argv)

    # Warming is pointless without the shared cache: otherwise `default_price_clients` builds
    # process-local TTL clients whose snapshots die with this CronJob pod. Fail fast instead.
    if market_cache_config_from_env() is None:
        raise RuntimeError(
            "AUGUR_MARKET_CACHE_URL (or AUGUR_CACHE_URL) must be set: warming a process-local cache "
            "that dies with this process serves nobody."
        )

    catalog = MarketCatalog.from_yaml(args.catalog)
    async with default_price_clients() as price_clients:
        summary = await warm_market_cache(catalog, price_clients)
    logger.info(
        "warmed market cache: %d/%d markets succeeded, %d failed", summary.succeeded, summary.requested, summary.failed
    )
    # Exit nonzero only when nothing succeeded (cache unreachable / every upstream down) so the
    # CronJob surfaces a hard outage; a few flapping markets still leave the pass green.
    return 0 if summary.succeeded > 0 or summary.requested == 0 else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_run(argv))


if __name__ == "__main__":
    sys.exit(main())
