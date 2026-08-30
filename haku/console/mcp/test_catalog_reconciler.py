from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest_bazel
from mcp import types as mcp_types

from haku.console.mcp.approval import DegradedReflection, ReflectionFailureStage
from haku.console.mcp.catalog_reconciler import OperatorCatalogReconciler
from haku.console.mcp.reflection_cache import ReflectedCatalog
from haku.console.mcp_config import McpServerEntry, NoCredential, RemoteMcpBackend, _server_catalog_refresh_interval
from haku.console.notifications.console_events import ConnectionStatus, McpOperatorAuthChangedEvent


def _server(server_id: str, *, refresh_interval: float | None = None) -> McpServerEntry:
    return McpServerEntry(
        id=server_id,
        backend=RemoteMcpBackend(url=f"https://{server_id}.invalid/mcp", auth=NoCredential()),
        catalog_refresh_interval_seconds=refresh_interval,
    )


def _reconciler(
    *, operator_ids: AsyncMock, metadata: AsyncMock, interval: float = 60.0, servers: list[str] | None = None
) -> OperatorCatalogReconciler:
    dispatcher = Mock()
    dispatcher.metadata = metadata
    return OperatorCatalogReconciler(
        servers=[_server(server_id) for server_id in (servers or ["alpha", "beta"])],
        dispatcher=dispatcher,
        oauth_store=Mock(),
        provider_store=Mock(),
        operator_ids=operator_ids,
        refresh_interval_seconds=interval,
    )


async def test_run_publishes_complete_catalog_before_becoming_ready() -> None:
    operator_id = UUID(int=42)
    metadata = AsyncMock(
        side_effect=[
            ReflectedCatalog(tools=[mcp_types.Tool(name="alpha_tool", inputSchema={"type": "object"})]),
            ReflectedCatalog(tools=[mcp_types.Tool(name="beta_tool", inputSchema={"type": "object"})]),
        ]
    )
    catalogs = _reconciler(operator_ids=AsyncMock(return_value=[operator_id]), metadata=metadata)

    async with catalogs.run():
        alpha = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
        beta = catalogs.metadata(operator_id=operator_id, server=_server("beta"))

    assert isinstance(alpha, ReflectedCatalog)
    assert isinstance(beta, ReflectedCatalog)
    assert [tool.name for tool in alpha.tools] == ["alpha_tool"]
    assert [tool.name for tool in beta.tools] == ["beta_tool"]
    assert metadata.await_count == 2


async def test_snapshot_reads_do_not_reflect_and_are_detached() -> None:
    operator_id = UUID(int=42)
    metadata = AsyncMock(
        side_effect=[
            ReflectedCatalog(tools=[mcp_types.Tool(name="alpha_tool", inputSchema={"type": "object"})]),
            DegradedReflection(failure_stage=ReflectionFailureStage.TOOL_DISCOVERY, degraded_reason="offline"),
        ]
    )
    catalogs = _reconciler(operator_ids=AsyncMock(return_value=[operator_id]), metadata=metadata)
    await catalogs.reconcile()
    metadata.reset_mock()

    first = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
    assert isinstance(first, ReflectedCatalog)
    first.tools[0].inputSchema["mutated"] = True
    second = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))

    assert isinstance(second, ReflectedCatalog)
    assert "mutated" not in second.tools[0].inputSchema
    assert isinstance(catalogs.metadata(operator_id=operator_id, server=_server("beta")), DegradedReflection)
    metadata.assert_not_awaited()


def test_server_refresh_interval_overrides_the_default() -> None:
    assert _server_catalog_refresh_interval(_server("github", refresh_interval=900.0), 60.0) == 900.0
    assert _server_catalog_refresh_interval(_server("grocy"), 60.0) == 60.0


async def test_refreshing_one_server_does_not_refresh_unrelated_servers() -> None:
    operator_id = UUID(int=42)
    metadata = AsyncMock(
        side_effect=[
            ReflectedCatalog(tools=[mcp_types.Tool(name="alpha_old", inputSchema={"type": "object"})]),
            ReflectedCatalog(tools=[mcp_types.Tool(name="beta_old", inputSchema={"type": "object"})]),
            ReflectedCatalog(tools=[mcp_types.Tool(name="alpha_new", inputSchema={"type": "object"})]),
        ]
    )
    catalogs = _reconciler(operator_ids=AsyncMock(return_value=[operator_id]), metadata=metadata)
    await catalogs.reconcile()
    await catalogs.refresh_server(_server("alpha"))

    alpha = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
    beta = catalogs.metadata(operator_id=operator_id, server=_server("beta"))
    assert isinstance(alpha, ReflectedCatalog)
    assert isinstance(beta, ReflectedCatalog)
    assert [tool.name for tool in alpha.tools] == ["alpha_new"]
    assert [tool.name for tool in beta.tools] == ["beta_old"]
    assert metadata.await_count == 3


async def test_unseen_operator_is_refreshed_without_blocking_first_read() -> None:
    operator_id = UUID(int=99)
    reflected = asyncio.Event()

    async def metadata(*args: object, **kwargs: object) -> ReflectedCatalog:
        _ = args, kwargs
        reflected.set()
        return ReflectedCatalog(tools=[mcp_types.Tool(name="ready", inputSchema={"type": "object"})])

    catalogs = _reconciler(operator_ids=AsyncMock(return_value=[]), metadata=AsyncMock(side_effect=metadata))

    first = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
    assert isinstance(first, DegradedReflection)
    await asyncio.wait_for(reflected.wait(), timeout=1.0)
    for _ in range(10):
        current = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
        if isinstance(current, ReflectedCatalog):
            break
        await asyncio.sleep(0)
    assert isinstance(current, ReflectedCatalog)


async def test_connection_change_invalidates_before_refreshing() -> None:
    operator_id = UUID(int=42)
    metadata = AsyncMock(
        side_effect=[
            ReflectedCatalog(tools=[mcp_types.Tool(name="old", inputSchema={"type": "object"})]),
            ReflectedCatalog(tools=[]),
            ReflectedCatalog(tools=[mcp_types.Tool(name="new", inputSchema={"type": "object"})]),
            ReflectedCatalog(tools=[]),
        ]
    )
    catalogs = _reconciler(operator_ids=AsyncMock(return_value=[operator_id]), metadata=metadata)
    await catalogs.reconcile()

    catalogs.connection_changed(
        operator_id, McpOperatorAuthChangedEvent(server_id="alpha", status=ConnectionStatus.DISCONNECTED)
    )
    assert isinstance(catalogs.metadata(operator_id=operator_id, server=_server("alpha")), DegradedReflection)

    for _ in range(10):
        current = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
        if isinstance(current, ReflectedCatalog):
            break
        await asyncio.sleep(0)
    assert isinstance(current, ReflectedCatalog)
    assert [tool.name for tool in current.tools] == ["new"]


async def test_pre_change_refresh_cannot_republish_an_invalidated_generation() -> None:
    operator_id = UUID(int=42)
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    calls = 0

    async def metadata(*args: object, **kwargs: object) -> ReflectedCatalog:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            old_started.set()
            await release_old.wait()
            return ReflectedCatalog(tools=[mcp_types.Tool(name="old", inputSchema={"type": "object"})])
        return ReflectedCatalog(tools=[mcp_types.Tool(name="new", inputSchema={"type": "object"})])

    catalogs = _reconciler(
        operator_ids=AsyncMock(return_value=[operator_id]), metadata=AsyncMock(side_effect=metadata), servers=["alpha"]
    )
    stale_refresh = asyncio.create_task(catalogs.refresh_operator(operator_id))
    await old_started.wait()

    catalogs.connection_changed(
        operator_id, McpOperatorAuthChangedEvent(server_id="alpha", status=ConnectionStatus.DISCONNECTED)
    )
    for _ in range(10):
        current = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
        if isinstance(current, ReflectedCatalog):
            break
        await asyncio.sleep(0)
    assert isinstance(current, ReflectedCatalog)
    assert [tool.name for tool in current.tools] == ["new"]

    release_old.set()
    await stale_refresh
    current = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))
    assert isinstance(current, ReflectedCatalog)
    assert [tool.name for tool in current.tools] == ["new"]


if __name__ == "__main__":
    pytest_bazel.main()
