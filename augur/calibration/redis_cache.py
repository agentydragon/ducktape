"""Redis-backed read-through cache for prediction-market snapshots."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from functools import partial
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.valkey import ValkeyStore
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from augur.calibration.platform import Market, Platform, PriceClient

_LOGGER = logging.getLogger(__name__)

_CACHE_COLLECTION = "augur-market-cache-v1"
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

    schema_version: int = Field(default=1)
    platform: Platform
    market_id: str
    fetched_at_epoch_seconds: float
    market: dict[str, Any]


class AsyncKeyValueContext(AbstractAsyncContextManager[AsyncKeyValue], Protocol):
    async def __aenter__(self) -> AsyncKeyValue: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None: ...


StoreFactory = Callable[[], AsyncKeyValueContext]


class ValkeyStoreContext:
    def __init__(self, config: RedisMarketCacheConfig) -> None:
        self._config = config
        self._store: ValkeyStore | None = None

    async def __aenter__(self) -> AsyncKeyValue:
        store = ValkeyStore(
            host=self._config.host,
            port=self._config.port,
            db=self._config.db,
            username=self._config.username,
            password=self._config.password,
            default_collection=_CACHE_COLLECTION,
        )
        self._store = store
        return await store.__aenter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        if self._store is None:
            return None
        return await self._store.__aexit__(exc_type, exc, tb)


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
    clients: Mapping[Platform, PriceClient], config: RedisMarketCacheConfig, *, clock: Callable[[], float] = time.time
) -> dict[Platform, PriceClient]:
    return {
        platform: RedisCachingPriceClient(
            platform=platform,
            upstream=client,
            store_factory=partial(ValkeyStoreContext, config),
            ttl_seconds=config.ttl_seconds,
            retention_seconds=config.retention_seconds,
            clock=clock,
        )
        for platform, client in clients.items()
    }


class RedisCachingPriceClient:
    """Sync ``PriceClient`` wrapper backed by Redis/Valkey.

    The platform clients are synchronous and the Valkey client available in this repo is async.
    Server endpoints that call ``PriceClient`` are sync FastAPI handlers, so each cache operation is
    bridged with ``asyncio.run`` and uses a short-lived Valkey context.
    """

    def __init__(
        self,
        *,
        platform: Platform,
        upstream: PriceClient,
        store_factory: StoreFactory,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        retention_seconds: int = _DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._platform = platform
        self._upstream = upstream
        self._store_factory = store_factory
        self._ttl_seconds = ttl_seconds
        self._retention_seconds = retention_seconds
        self._clock = clock

    def get_market(self, market_id: str) -> Market:
        cached = self._load(market_id)
        now = self._clock()
        if cached is not None and now - cached.fetched_at_epoch_seconds <= self._ttl_seconds:
            return _market_from_snapshot(cached)

        try:
            market = self._upstream.get_market(market_id)
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

        self._store(
            MarketSnapshot(
                platform=self._platform,
                market_id=market_id,
                fetched_at_epoch_seconds=now,
                market=_market_to_dict(market),
            )
        )
        return market

    def close(self) -> None:
        self._upstream.close()

    def _load(self, market_id: str) -> MarketSnapshot | None:
        try:
            raw = asyncio.run(self._async_load(market_id))
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
        if snapshot.schema_version != 1 or snapshot.platform != self._platform or snapshot.market_id != market_id:
            _LOGGER.warning("ignoring mismatched cached %s market %r", self._platform.value, market_id)
            return None
        return snapshot

    async def _async_load(self, market_id: str) -> dict[str, Any] | None:
        async with self._store_factory() as store:
            return await store.get(_cache_key(self._platform, market_id))

    def _store(self, snapshot: MarketSnapshot) -> None:
        try:
            asyncio.run(self._async_store(snapshot))
        except Exception:
            _LOGGER.warning(
                "failed to write cached %s market %r", self._platform.value, snapshot.market_id, exc_info=True
            )

    async def _async_store(self, snapshot: MarketSnapshot) -> None:
        async with self._store_factory() as store:
            await store.put(
                _cache_key(self._platform, snapshot.market_id),
                snapshot.model_dump(mode="json"),
                ttl=self._retention_seconds,
            )


def _cache_key(platform: Platform, market_id: str) -> str:
    return f"{platform.value}:{market_id}"


def _market_to_dict(market: Market) -> dict[str, Any]:
    return {
        "id": market.id,
        "url": market.url,
        "probability": market.probability,
        "volume": market.volume,
        "volume_unit": market.volume_unit,
        "title": market.title,
        "rules": market.rules,
    }


def _market_from_snapshot(snapshot: MarketSnapshot) -> Market:
    return Market(**snapshot.market)


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
