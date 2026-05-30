"""Hermetic tests for `ManifoldClient`: probability wrapper + TTL cache.

A `MockTransport`-backed client answers market reads without network. Caching is proven
with a fake mutable clock and a handler that counts how many reads actually hit the
transport, so the two within-TTL reads collapse to one network call and advancing the
clock past the TTL forces a refetch.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from augur.calibration.manifold import ManifoldClient
from augur.calibration.testing import mock_manifold_client


def test_fetch_yes_probability_returns_market_probability() -> None:
    client = mock_manifold_client({"AAA": 0.42})
    assert client.fetch_yes_probability("AAA") == 0.42
    assert client.get_market("AAA").id == "AAA"


def test_fetch_yes_probability_raises_when_probability_missing() -> None:
    # A market with no `probability` (e.g. a non-binary market) has no YES probability,
    # so the wrapper raises rather than inventing one.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "NOPROB", "url": "https://manifold.markets/test/NOPROB"})

    client = ManifoldClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get_market("NOPROB").probability is None
    with pytest.raises(ValueError, match="no YES probability"):
        client.fetch_yes_probability("NOPROB")


def test_get_market_caches_within_ttl_and_refetches_after() -> None:
    now = [1000.0]
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"id": "AAA", "url": "https://manifold.markets/test/AAA", "probability": 0.3})

    client = ManifoldClient(
        cache_ttl_seconds=120.0, clock=lambda: now[0], client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    # First read hits the network; a second read inside the TTL is served from cache.
    assert client.get_market("AAA").probability == 0.3
    assert calls["count"] == 1
    now[0] += 119.0
    assert client.get_market("AAA").probability == 0.3
    assert calls["count"] == 1

    # Crossing the TTL forces a fresh network read.
    now[0] += 2.0
    assert client.get_market("AAA").probability == 0.3
    assert calls["count"] == 2


if __name__ == "__main__":
    pytest_bazel.main()
