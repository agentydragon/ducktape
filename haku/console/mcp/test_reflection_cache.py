"""Upstream catalog reuse: TTL, single-flight, and the credential boundary."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel
from mcp import types as mcp_types

from haku.console.mcp.reflection_cache import ReflectedCatalog, ReflectionCache, ReflectionCacheKey

# Long enough that nothing expires mid-test; expiry itself is covered by the zero-TTL cases.
NEVER_EXPIRES = 3600.0


def _key(server_id: str = "grocy", credential: str = "token-a") -> ReflectionCacheKey:
    return ReflectionCacheKey(server_id=server_id, config_fingerprint="cfg", credential_fingerprint=credential)


def _tools(*names: str) -> list[mcp_types.Tool]:
    return [mcp_types.Tool(name=name, inputSchema={"type": "object"}) for name in names]


class _CountingReflector:
    def __init__(self, tools: list[mcp_types.Tool] | None = None) -> None:
        self.calls = 0
        self._tools = tools if tools is not None else _tools("stock_add")

    async def __call__(self) -> ReflectedCatalog:
        self.calls += 1
        return ReflectedCatalog(tools=self._tools, instructions="how to use me")


async def test_second_reflection_within_ttl_reuses_the_first() -> None:
    cache = ReflectionCache(NEVER_EXPIRES)
    reflect = _CountingReflector()

    first = await cache.reflect(_key(), reflect)
    second = await cache.reflect(_key(), reflect)

    assert [tool.name for tool in second.tools] == [tool.name for tool in first.tools]
    assert reflect.calls == 1


async def test_zero_ttl_reflects_again_on_the_next_request() -> None:
    cache = ReflectionCache(0.0)
    reflect = _CountingReflector()

    await cache.reflect(_key(), reflect)
    await cache.reflect(_key(), reflect)

    assert reflect.calls == 2


async def test_concurrent_reflections_of_one_server_collapse_into_a_single_upstream_call() -> None:
    """The reason single-flight matters: one MCP handshake opens several connections at once, so
    without it a client's first `tools/list` fans out to every upstream more than once."""
    cache = ReflectionCache(0.0)  # zero TTL: only single-flight can collapse these.
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def reflect() -> ReflectedCatalog:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ReflectedCatalog(tools=_tools("stock_add"))

    waiters = [asyncio.create_task(cache.reflect(_key(), reflect)) for _ in range(4)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*waiters)

    assert calls == 1
    assert all([tool.name for tool in result.tools] == ["stock_add"] for result in results)


async def test_a_caller_mutating_the_returned_catalog_cannot_corrupt_the_cache() -> None:
    """Every caller gets its own list. The cached one is handed to every later caller too, so a
    consumer that sorts or filters in place would otherwise change what everyone else sees."""
    cache = ReflectionCache(NEVER_EXPIRES)
    reflect = _CountingReflector(_tools("stock_add", "echo"))

    first = await cache.reflect(_key(), reflect)
    first.tools.clear()
    second = await cache.reflect(_key(), reflect)

    assert [tool.name for tool in second.tools] == ["stock_add", "echo"]
    assert reflect.calls == 1


async def test_a_caller_mutating_a_returned_tool_cannot_corrupt_the_cache() -> None:
    """The nested half, which a new list alone does not cover: a `Tool` is mutable and its
    `inputSchema` is a plain dict that `_build_proxy_tool` hands straight to a passthrough tool."""
    cache = ReflectionCache(NEVER_EXPIRES)
    reflect = _CountingReflector()

    (borrowed,) = (await cache.reflect(_key(), reflect)).tools
    borrowed.name = "renamed"
    borrowed.inputSchema["properties"] = {"injected": {"type": "string"}}
    (fresh,) = (await cache.reflect(_key(), reflect)).tools

    assert fresh.name == "stock_add"
    assert fresh.inputSchema == {"type": "object"}
    assert reflect.calls == 1


async def test_a_different_credential_does_not_reuse_the_cached_catalog() -> None:
    """The fail-closed property: reflection is request-local so a client cannot keep calling tools
    after its Operator disconnects a server. A catalog must never outlive the credential that read
    it."""
    cache = ReflectionCache(NEVER_EXPIRES)
    first = _CountingReflector(_tools("operator_a_tool"))
    second = _CountingReflector(_tools("operator_b_tool"))

    await cache.reflect(_key(credential="token-a"), first)
    other = await cache.reflect(_key(credential="token-b"), second)

    assert [tool.name for tool in other.tools] == ["operator_b_tool"]
    assert second.calls == 1


async def test_a_changed_server_config_does_not_reuse_the_cached_catalog() -> None:
    cache = ReflectionCache(NEVER_EXPIRES)
    reflect = _CountingReflector()

    await cache.reflect(
        ReflectionCacheKey(server_id="grocy", config_fingerprint="a", credential_fingerprint="t"), reflect
    )
    await cache.reflect(
        ReflectionCacheKey(server_id="grocy", config_fingerprint="b", credential_fingerprint="t"), reflect
    )

    assert reflect.calls == 2


async def test_a_failed_reflection_is_not_cached_so_a_recovered_server_is_retried() -> None:
    cache = ReflectionCache(NEVER_EXPIRES)
    attempts = 0

    async def reflect() -> ReflectedCatalog:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("upstream unreachable")
        return ReflectedCatalog(tools=_tools("stock_add"))

    with pytest.raises(RuntimeError, match="upstream unreachable"):
        await cache.reflect(_key(), reflect)
    recovered = await cache.reflect(_key(), reflect)

    assert [tool.name for tool in recovered.tools] == ["stock_add"]
    assert attempts == 2


async def test_one_caller_giving_up_does_not_cancel_the_shared_reflection() -> None:
    """A client that disconnects mid-listing must not take the in-flight reflection down with it."""
    cache = ReflectionCache(NEVER_EXPIRES)
    started = asyncio.Event()
    release = asyncio.Event()

    async def reflect() -> ReflectedCatalog:
        started.set()
        await release.wait()
        return ReflectedCatalog(tools=_tools("stock_add"))

    abandoned = asyncio.create_task(cache.reflect(_key(), reflect))
    patient = asyncio.create_task(cache.reflect(_key(), reflect))
    await started.wait()
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    release.set()

    assert [tool.name for tool in (await patient).tools] == ["stock_add"]


if __name__ == "__main__":
    pytest_bazel.main()
