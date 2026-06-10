"""Redis-backed read-through cache for prediction-market snapshots."""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import unquote, urlparse

from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.valkey import ValkeyStore
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from finance.augur.calibration.platform import Market, PriceClient
from finance.augur.calibration.quote import BookQuote, PoolQuote, Quote
from finance.evidence.markets import Platform

_LOGGER = logging.getLogger(__name__)

# v2: snapshots store the structured `quote` (book/pool), not the legacy single `probability`.
_CACHE_COLLECTION = "augur-market-cache-v2"
_CACHE_SCHEMA_VERSION = 2
_DEFAULT_REDIS_PORT = 6379
_DEFAULT_TTL_SECONDS = 12 * 60 * 60
_DEFAULT_RETENTION_SECONDS = 48 * 60 * 60
_MARKET_CACHE_URL_ENV = "AUGUR_MARKET_CACHE_URL"
_GENERIC_CACHE_URL_ENV = "AUGUR_CACHE_URL"
_TTL_ENV = "AUGUR_MARKET_CACHE_TTL_SECONDS"
_RETENTION_ENV = "AUGUR_MARKET_CACHE_RETENTION_SECONDS"


class RedisMarketCacheConfig(BaseModel):
    """Connection and freshness settings for the market cache."""

    model_config = ConfigDict(frozen=True)

    host: str
    port: int = _DEFAULT_REDIS_PORT
    db: int = 0
    username: str | None = None
    password: str | None = None
    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    retention_seconds: int = _DEFAULT_RETENTION_SECONDS


class MarketSnapshot(BaseModel):
    """Serialized market state stored in Redis."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=_CACHE_SCHEMA_VERSION)
    platform: Platform
    market_id: str
    fetched_at_epoch_seconds: float
    market: dict[str, Any]


def open_market_cache_store(config: RedisMarketCacheConfig) -> AbstractAsyncContextManager[AsyncKeyValue]:
    """One long-lived Valkey store for the process: `async with` it once (server lifespan / CLI
    run) and share the entered store across every platform's caching client, instead of opening a
    fresh connection per cache read/write."""
    return ValkeyStore(
        host=config.host,
        port=config.port,
        db=config.db,
        username=config.username,
        password=config.password,
        default_collection=_CACHE_COLLECTION,
    )


def market_cache_config_from_env(env: Mapping[str, str] = os.environ) -> RedisMarketCacheConfig | None:
    """Return Redis cache config when enabled by env, otherwise ``None``.

    ``AUGUR_MARKET_CACHE_URL`` is the specific knob for this cache. ``AUGUR_CACHE_URL`` is also
    accepted so the same Redis/Valkey instance can later back non-market Augur caches.
    """

    raw_url = env.get(_MARKET_CACHE_URL_ENV) or env.get(_GENERIC_CACHE_URL_ENV)
    if raw_url is None:
        return None

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"redis", "valkey"}:
        raise ValueError(
            f"{_MARKET_CACHE_URL_ENV} / {_GENERIC_CACHE_URL_ENV} must use redis:// or valkey://, got {parsed.scheme!r}"
        )
    if parsed.hostname is None:
        raise ValueError(f"Cache URL {raw_url!r} must include a host")

    ttl_seconds = _float_env(env, _TTL_ENV, _DEFAULT_TTL_SECONDS)
    retention_seconds = _int_env(env, _RETENTION_ENV, _DEFAULT_RETENTION_SECONDS)
    retention_seconds = max(retention_seconds, math.ceil(ttl_seconds))
    return RedisMarketCacheConfig(
        host=parsed.hostname,
        port=parsed.port or _DEFAULT_REDIS_PORT,
        db=_parse_db(parsed.path),
        username=unquote(parsed.username) if parsed.username is not None else None,
        password=unquote(parsed.password) if parsed.password is not None else None,
        ttl_seconds=ttl_seconds,
        retention_seconds=retention_seconds,
    )


def wrap_price_clients_with_redis_cache(
    clients: Mapping[Platform, PriceClient],
    config: RedisMarketCacheConfig,
    store: AsyncKeyValue,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[Platform, PriceClient]:
    """Wrap each upstream client in a read-through cache over the shared, already-open `store`."""
    return {
        platform: RedisCachingPriceClient(
            platform=platform,
            upstream=client,
            store=store,
            ttl_seconds=config.ttl_seconds,
            retention_seconds=config.retention_seconds,
            clock=clock,
        )
        for platform, client in clients.items()
    }


class RedisCachingPriceClient:
    """Async ``PriceClient`` wrapper backed by a long-lived Valkey ``store``.

    Reads/writes go through the one persistent store handed in at construction — no per-call
    client creation, no ``asyncio.run`` bridge. Cache read/write failures are best-effort: a read
    miss or a store error degrades to the upstream fetch rather than failing the request.
    """

    def __init__(
        self,
        *,
        platform: Platform,
        upstream: PriceClient,
        store: AsyncKeyValue,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        retention_seconds: int = _DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._platform = platform
        self._upstream = upstream
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._retention_seconds = retention_seconds
        self._clock = clock

    async def get_market(self, market_id: str) -> Market:
        cached = await self._load(market_id)
        now = self._clock()
        if cached is not None and now - cached.fetched_at_epoch_seconds <= self._ttl_seconds:
            return _market_from_snapshot(cached)

        try:
            market = await self._upstream.get_market(market_id)
        except Exception:
            if cached is not None:
                _LOGGER.warning(
                    "serving stale cached %s market %r after upstream fetch failed",
                    self._platform.value,
                    market_id,
                    exc_info=True,
                )
                return _market_from_snapshot(cached)
            raise

        await self._store_snapshot(
            MarketSnapshot(
                platform=self._platform,
                market_id=market_id,
                fetched_at_epoch_seconds=now,
                market=_market_to_dict(market),
            )
        )
        return market

    async def aclose(self) -> None:
        await self._upstream.aclose()

    async def _load(self, market_id: str) -> MarketSnapshot | None:
        try:
            raw = await self._store.get(_cache_key(self._platform, market_id))
        # Cache reads are best-effort: a store/connection error must fall through to the upstream
        # fetch, not fail the request. Broad except is intentional here.
        except Exception:
            _LOGGER.warning("failed to read cached %s market %r", self._platform.value, market_id, exc_info=True)
            return None
        if raw is None:
            return None
        try:
            snapshot = MarketSnapshot.model_validate(raw)
        except ValidationError:
            _LOGGER.warning("ignoring invalid cached %s market %r", self._platform.value, market_id, exc_info=True)
            return None
        if (
            snapshot.schema_version != _CACHE_SCHEMA_VERSION
            or snapshot.platform != self._platform
            or snapshot.market_id != market_id
        ):
            _LOGGER.warning("ignoring mismatched cached %s market %r", self._platform.value, market_id)
            return None
        return snapshot

    async def _store_snapshot(self, snapshot: MarketSnapshot) -> None:
        try:
            await self._store.put(
                _cache_key(self._platform, snapshot.market_id),
                snapshot.model_dump(mode="json"),
                ttl=self._retention_seconds,
            )
        # Cache writes are best-effort: a store error must not fail an otherwise-successful fetch.
        except Exception:
            _LOGGER.warning(
                "failed to write cached %s market %r", self._platform.value, snapshot.market_id, exc_info=True
            )


def _cache_key(platform: Platform, market_id: str) -> str:
    return f"{platform.value}:{market_id}"


def _market_to_dict(market: Market) -> dict[str, Any]:
    return {
        "id": market.id,
        "url": market.url,
        "quote": _quote_to_dict(market.quote),
        "volume": market.volume,
        "volume_unit": market.volume_unit,
        "title": market.title,
        "rules": market.rules,
    }


def _quote_to_dict(quote: Quote) -> dict[str, Any] | None:
    match quote:
        case BookQuote():
            return {
                "kind": "book",
                "bid": quote.bid,
                "ask": quote.ask,
                "bid_size": quote.bid_size,
                "ask_size": quote.ask_size,
                "last_trade": quote.last_trade,
            }
        case PoolQuote(price=price):
            return {"kind": "pool", "price": price}
        case None:
            return None


def _quote_from_dict(data: dict[str, Any] | None) -> Quote:
    if data is None:
        return None
    match data["kind"]:
        case "book":
            return BookQuote(
                bid=data["bid"],
                ask=data["ask"],
                bid_size=data["bid_size"],
                ask_size=data["ask_size"],
                last_trade=data["last_trade"],
            )
        case "pool":
            return PoolQuote(price=data["price"])
        case other:
            raise ValueError(f"unknown cached quote kind {other!r}")


def _market_from_snapshot(snapshot: MarketSnapshot) -> Market:
    data = snapshot.market
    return Market(
        id=data["id"],
        url=data["url"],
        quote=_quote_from_dict(data["quote"]),
        volume=data.get("volume"),
        volume_unit=data.get("volume_unit"),
        title=data.get("title"),
        rules=data.get("rules"),
    )


def _parse_db(path: str) -> int:
    if path in {"", "/"}:
        return 0
    return int(path.removeprefix("/"))


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    return default if raw is None else float(raw)


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    return default if raw is None else int(raw)
