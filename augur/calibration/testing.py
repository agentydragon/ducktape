"""Hermetic `ManifoldClient` for tests (mirrors `augur.model.testing`).

`mock_manifold_client` builds a real :class:`ManifoldClient` over an
``httpx.MockTransport`` that answers each market read from a fixed price map, so
calibration runs stay hermetic (no network) while still exercising the client's real
caching/parsing path. The `clock` / `cache_ttl_seconds` seams thread straight through to
the client for tests that drive the TTL cache.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import httpx

from augur.calibration.manifold import ManifoldClient


def mock_manifold_client(
    prices: Mapping[str, float], *, clock: Callable[[], float] = time.monotonic, cache_ttl_seconds: float = 120.0
) -> ManifoldClient:
    """A `ManifoldClient` whose market reads resolve from `prices` keyed by Manifold id."""

    def handler(request: httpx.Request) -> httpx.Response:
        market_id = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "id": market_id,
                "url": f"https://manifold.markets/test/{market_id}",
                "probability": prices[market_id],
            },
        )

    return ManifoldClient(
        clock=clock, cache_ttl_seconds=cache_ttl_seconds, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
