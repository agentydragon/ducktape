"""Short-lived reuse of reflected upstream MCP tool catalogs.

Every `tools/list` against the console fans out to each configured server and reflects it
live, and each reflection is a fresh MCP connect: transport, `initialize`, `tools/list`,
teardown. The fan-out runs concurrently, so the cost of a listing is its slowest upstream --
and an upstream reached over its public URL costs several times one reached in-cluster.
`stateless_http=True` means the whole thing is paid again on every request, with no session
to amortize it over.

Two separate wins, and the second is the larger one in practice: a TTL lets a burst of
listings reuse one reflection, and single-flight collapses *concurrent* listings of the same
server into a single upstream call. An MCP client opening several connections during one
handshake is the normal case, not an edge case.

**Only successful reflections are stored.** A failure propagates to the caller, which turns it
into a `DegradedReflection` -- so a server that has recovered is retried on the very next
listing rather than staying degraded for the TTL.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcp import types as mcp_types


@dataclass(frozen=True, slots=True)
class ReflectionCacheKey:
    """Everything a cached catalog is valid for.

    `credential_fingerprint` is what keeps discovery fail-closed. Reflection is deliberately
    request-local so a client cannot call a tool after its Operator disconnects that server
    (see `OperatorToolProvider`), and a cache keyed only by server id would reintroduce exactly
    that hole. Because credentials are resolved *before* the cache is consulted, a disconnected
    Operator never reaches a cached entry at all, and a rotated or refreshed credential lands on
    a different key instead of reusing the previous holder's catalog.
    """

    server_id: str
    # Covers URL, backend kind, and auth shape, so an edited config invalidates on reload.
    config_fingerprint: str
    # A digest, never the credential itself.
    credential_fingerprint: str


@dataclass(frozen=True, slots=True)
class _CachedCatalog:
    tools: list[mcp_types.Tool]
    expires_at: float


class ReflectionCache:
    """Per-replica TTL cache with single-flight.

    Per-replica is deliberate: the console runs multiple replicas behind a Service with no
    session affinity, and a catalog is cheap to re-derive, so each replica warming its own copy
    is simpler than shared state and has no correctness cost.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._catalogs: dict[ReflectionCacheKey, _CachedCatalog] = {}
        self._in_flight: dict[ReflectionCacheKey, asyncio.Task[list[mcp_types.Tool]]] = {}

    async def reflect(
        self, key: ReflectionCacheKey, load: Callable[[], Awaitable[list[mcp_types.Tool]]]
    ) -> list[mcp_types.Tool]:
        """Return a fresh cached catalog, join an in-flight reflection, or start one."""
        cached = self._catalogs.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            return list(cached.tools)
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._load(key, load))
            self._in_flight[key] = task
        # Shielded so one caller giving up (client disconnect, an outer timeout) does not cancel
        # the reflection every other caller is waiting on.
        tools = await asyncio.shield(task)
        return list(tools)

    async def _load(
        self, key: ReflectionCacheKey, load: Callable[[], Awaitable[list[mcp_types.Tool]]]
    ) -> list[mcp_types.Tool]:
        try:
            tools = list(await load())
            self._prune()
            self._catalogs[key] = _CachedCatalog(tools=tools, expires_at=time.monotonic() + self._ttl_seconds)
            return tools
        finally:
            # Also on failure: a raise must not wedge the key into permanent single-flight.
            self._in_flight.pop(key, None)

    def _prune(self) -> None:
        """Drop expired entries so rotated credentials and removed servers don't accumulate keys."""
        now = time.monotonic()
        for key in [key for key, entry in self._catalogs.items() if entry.expires_at <= now]:
            del self._catalogs[key]
