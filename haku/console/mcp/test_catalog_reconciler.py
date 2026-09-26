from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest_bazel
from mcp import types as mcp_types

from haku.console.mcp.approval import DegradedReflection, ReflectionFailureStage
from haku.console.mcp.catalog_reconciler import OperatorCatalogReconciler
from haku.console.mcp.reflection_cache import ReflectedCatalog
from haku.console.mcp_config import InProcessBackend, McpServerEntry, NoCredential


def _server(server_id: str) -> McpServerEntry:
    return McpServerEntry(id=server_id, backend=InProcessBackend(credential=NoCredential()))


def _reconciler(
    *, operator_ids: AsyncMock, metadata: AsyncMock, interval: float = 60.0, servers: list[str] | None = None
) -> OperatorCatalogReconciler:
    dispatcher = Mock()
    dispatcher.metadata = metadata
    return OperatorCatalogReconciler(
        servers=[_server(server_id) for server_id in (servers or ["alpha", "beta"])],
        dispatcher=dispatcher,
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
    first.tools[0].input_schema["mutated"] = True
    second = catalogs.metadata(operator_id=operator_id, server=_server("alpha"))

    assert isinstance(second, ReflectedCatalog)
    assert "mutated" not in second.tools[0].input_schema
    assert isinstance(catalogs.metadata(operator_id=operator_id, server=_server("beta")), DegradedReflection)
    metadata.assert_not_awaited()


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


if __name__ == "__main__":
    pytest_bazel.main()
