from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel

from augur.calibration.platform import Market, Platform
from augur.calibration.redis_cache import MarketSnapshot, RedisCachingPriceClient, market_cache_config_from_env


class _FakeStore:
    """Minimal in-memory ``AsyncKeyValue`` for the read-through cache, already 'open'."""

    def __init__(self, data: dict[str, dict[str, Any]], ttls: dict[str, int]) -> None:
        self._data = data
        self._ttls = ttls

    async def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    async def put(self, key: str, value: dict[str, Any], ttl: int | None = None) -> None:
        self._data[key] = value
        if ttl is not None:
            self._ttls[key] = ttl


class _FakeUpstream:
    def __init__(self, market: Market | None = None, error: Exception | None = None) -> None:
        self.market = market
        self.error = error
        self.calls: list[str] = []
        self.closed = False

    async def get_market(self, market_id: str) -> Market:
        self.calls.append(market_id)
        if self.error is not None:
            raise self.error
        assert self.market is not None
        return self.market

    async def aclose(self) -> None:
        self.closed = True


async def test_fresh_cache_hit_avoids_upstream_fetch() -> None:
    data = {
        "manifold:m1": _snapshot(
            market_id="m1",
            fetched_at=10.0,
            market=Market(id="m1", url="https://example.com/m1", probability=0.4, title="cached"),
        )
    }
    ttls: dict[str, int] = {}
    upstream = _FakeUpstream(error=RuntimeError("should not fetch"))
    client = _client(upstream=upstream, data=data, ttls=ttls, now=20.0, ttl_seconds=30.0)

    assert await client.get_market("m1") == Market(
        id="m1", url="https://example.com/m1", probability=0.4, title="cached"
    )
    assert upstream.calls == []


async def test_stale_cache_refreshes_from_upstream_and_updates_cache() -> None:
    data = {
        "manifold:m1": _snapshot(
            market_id="m1", fetched_at=10.0, market=Market(id="m1", url="https://example.com/stale", probability=0.4)
        )
    }
    ttls: dict[str, int] = {}
    upstream_market = Market(id="m1", url="https://example.com/fresh", probability=0.7, volume=12.0)
    upstream = _FakeUpstream(market=upstream_market)
    client = _client(upstream=upstream, data=data, ttls=ttls, now=50.0, ttl_seconds=30.0, retention_seconds=120)

    assert await client.get_market("m1") == upstream_market
    assert upstream.calls == ["m1"]
    assert MarketSnapshot.model_validate(data["manifold:m1"]).market["url"] == "https://example.com/fresh"
    assert ttls == {"manifold:m1": 120}


async def test_stale_cache_survives_upstream_failure() -> None:
    cached_market = Market(id="m1", url="https://example.com/stale", probability=0.4)
    data = {"manifold:m1": _snapshot(market_id="m1", fetched_at=10.0, market=cached_market)}
    ttls: dict[str, int] = {}
    upstream = _FakeUpstream(error=RuntimeError("upstream down"))
    client = _client(upstream=upstream, data=data, ttls=ttls, now=50.0, ttl_seconds=30.0)

    assert await client.get_market("m1") == cached_market
    assert upstream.calls == ["m1"]


async def test_cache_miss_propagates_upstream_failure() -> None:
    upstream = _FakeUpstream(error=RuntimeError("upstream down"))
    client = _client(upstream=upstream, data={}, ttls={}, now=50.0)

    with pytest.raises(RuntimeError, match="upstream down"):
        await client.get_market("m1")


async def test_invalid_cache_payload_is_ignored() -> None:
    data = {"manifold:m1": {"schema_version": 1, "platform": "kalshi", "market_id": "m1"}}
    ttls: dict[str, int] = {}
    upstream_market = Market(id="m1", url="https://example.com/fresh", probability=0.7)
    upstream = _FakeUpstream(market=upstream_market)
    client = _client(upstream=upstream, data=data, ttls=ttls, now=20.0)

    assert await client.get_market("m1") == upstream_market
    assert upstream.calls == ["m1"]


def test_market_cache_config_from_env_accepts_generic_valkey_url() -> None:
    config = market_cache_config_from_env(
        {
            "AUGUR_CACHE_URL": "valkey://user:p%40ss@cache.local:6380/2",
            "AUGUR_MARKET_CACHE_TTL_SECONDS": "60",
            "AUGUR_MARKET_CACHE_RETENTION_SECONDS": "30",
        }
    )

    assert config is not None
    assert config.host == "cache.local"
    assert config.port == 6380
    assert config.db == 2
    assert config.username == "user"
    assert config.password == "p@ss"
    assert config.ttl_seconds == 60
    assert config.retention_seconds == 60


def _client(
    *,
    upstream: _FakeUpstream,
    data: dict[str, dict[str, Any]],
    ttls: dict[str, int],
    now: float,
    ttl_seconds: float = 30.0,
    retention_seconds: int = 90,
) -> RedisCachingPriceClient:
    return RedisCachingPriceClient(
        platform=Platform.MANIFOLD,
        upstream=upstream,
        store=_FakeStore(data, ttls),
        ttl_seconds=ttl_seconds,
        retention_seconds=retention_seconds,
        clock=lambda: now,
    )


def _snapshot(*, market_id: str, fetched_at: float, market: Market) -> dict[str, Any]:
    return MarketSnapshot(
        platform=Platform.MANIFOLD,
        market_id=market_id,
        fetched_at_epoch_seconds=fetched_at,
        market={
            "id": market.id,
            "url": market.url,
            "probability": market.probability,
            "volume": market.volume,
            "volume_unit": market.volume_unit,
            "title": market.title,
            "rules": market.rules,
        },
    ).model_dump(mode="json")


if __name__ == "__main__":
    pytest_bazel.main()
