"""Acceptance fixture for the MCP-backed Executor.

Scenarios 1-3 and 5-6 drive `McpActionGroupExecutor` directly against an in-process real
`fastmcp.FastMCP()` server (a real MCP protocol exchange over an in-memory transport), because they
need deterministic control over exactly when a tool mutation becomes visible relative to a refresh
or a dispatch. Scenarios 4 and 7 drive the real `ActionService` end to end, since they exercise the
coordinator's single-dispatch/lease/no-retry guarantees rather than the adapter's own per-call logic.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest_bazel
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncEngine

from util.bazel.runfiles import get_required_path
from x.agentplane.action_service.catalog import ActionCatalog, ActionGroup, ActionIdentity, McpExecutorBinding
from x.agentplane.action_service.db import ActionConflictError, ActionStore, make_sessionmaker
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionState,
    DecisionInput,
    ExecutionLease,
    ExecutionRequest,
    ExecutionState,
    Principal,
    PrincipalRole,
    Verdict,
)
from x.agentplane.action_service.runtime import running_executor
from x.agentplane.action_service.service import ActionService

CALLER = Principal(issuer="test-workload", subject="sandbox-a", role=PrincipalRole.CALLER)
OPERATOR = Principal(issuer="test-bff", subject="operator", role=PrincipalRole.OPERATOR)
GROUP_KEY = "demo"
FAKE_SERVER = "_main/x/agentplane/action_service/test_fixtures/fake_mcp_server.py"


def _group() -> ActionGroup:
    return ActionGroup(
        title="Demo MCP group",
        description="Test-only ActionGroup backed by an in-process fastmcp server.",
        executor=McpExecutorBinding(kind="mcp", description="in-process test server"),
    )


def _request(*, action: ActionIdentity, arguments: dict[str, Any]) -> ExecutionRequest:
    return ExecutionRequest(
        request_id=uuid4(), action=action, arguments=arguments, origin={}, correlation={}, caller_principal=CALLER.key
    )


async def _allowed_execution(service: ActionService, *, idempotency_key: str) -> Any:
    view = await service.submit(
        ActionRequestInput(
            idempotency_key=idempotency_key,
            action=ActionIdentity(group=GROUP_KEY, name="echo_once"),
            arguments={"text": "hi"},
        ),
        CALLER,
    )
    await service.decide(
        view.id,
        DecisionInput(verdict=Verdict.ALLOW, expected_version=view.version, idempotency_key=f"{idempotency_key}-allow"),
        OPERATOR,
    )
    return view.id


async def _poll_state(store: ActionStore, request_id: Any, *, want: ActionState) -> None:
    for _ in range(200):
        view = await store.get(request_id, CALLER)
        if view.state is want:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never reached {want}")


async def test_start_mirrors_tool_catalog_into_the_action_group() -> None:
    mcp = FastMCP("demo")

    @mcp.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @mcp.tool
    def greet(name: str) -> str:
        """Greet someone."""
        return f"hello {name}"

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, mcp)
    await executor.start()
    try:
        assert set(group.actions) == {"add", "greet"}
        assert group.actions["add"].description == "Add two numbers."
        assert group.actions["add"].input_schema["required"] == ["a", "b"]
        assert group.available is True
    finally:
        await executor.close()


async def test_explicit_refresh_picks_up_added_removed_and_changed_tools() -> None:
    mcp = FastMCP("demo")

    @mcp.tool
    def stale(x: int) -> int:
        return x

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, mcp, catalog_refresh_interval=timedelta(hours=1))
    await executor.start()
    try:
        assert set(group.actions) == {"stale"}

        @mcp.tool(name="fresh")
        def fresh_v1(y: int) -> int:
            return y

        mcp.local_provider.remove_tool("stale")
        await executor.refresh_catalog()
        assert set(group.actions) == {"fresh"}
        assert group.actions["fresh"].input_schema["required"] == ["y"]

        mcp.local_provider.remove_tool("fresh")

        @mcp.tool(name="fresh")
        def fresh_v2(y: int, z: int) -> int:
            return y + z

        await executor.refresh_catalog()
        required = group.actions["fresh"].input_schema["required"]
        assert isinstance(required, list)
        assert set(required) == {"y", "z"}
    finally:
        await executor.close()


async def test_missed_notification_is_recovered_by_explicit_refresh() -> None:
    """`operations_and_access.md`: correctness must not depend on receiving the list-changed
    notification. This installed fastmcp version never emits one on `add_tool`/`remove_tool`, so
    the periodic background loop (parked far in the future here) cannot have refreshed either --
    the mirror only updates once `refresh_catalog()` is called explicitly."""
    mcp = FastMCP("demo")

    @mcp.tool
    def original(x: int) -> int:
        return x

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, mcp, catalog_refresh_interval=timedelta(hours=1))
    await executor.start()
    try:
        assert set(group.actions) == {"original"}

        @mcp.tool
        def added(y: int) -> int:
            return y

        await asyncio.sleep(0.05)  # give a wrongly-firing background refresh a chance to run
        assert set(group.actions) == {"original"}, "mirror must stay stale until refresh_catalog() is called"

        await executor.refresh_catalog()
        assert set(group.actions) == {"original", "added"}
    finally:
        await executor.close()


async def test_one_allowed_execution_calls_the_backend_tool_exactly_once(engine: AsyncEngine) -> None:
    mcp = FastMCP("demo")
    calls: list[dict[str, Any]] = []

    @mcp.tool
    def echo_once(text: str) -> dict[str, str]:
        calls.append({"text": text})
        return {"echoed": text}

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, mcp)
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store,
        ActionCatalog(groups={GROUP_KEY: group}),
        {GROUP_KEY: executor},
        lease_duration=timedelta(seconds=5),
        lease_sweep_interval=timedelta(seconds=1),
        executor_heartbeat_interval=timedelta(seconds=1),
        executor_health_timeout=timedelta(seconds=30),
    )
    await executor.start()
    await service.start()
    try:
        request_id = await _allowed_execution(service, idempotency_key="one-call")
        await _poll_state(store, request_id, want=ActionState.SUCCEEDED)
        assert calls == [{"text": "hi"}]
        view = await store.get(request_id, CALLER)
        assert view.execution is not None
        assert view.execution.result == {"echoed": "hi"}

        # A duplicate Decision on the same already-decided request must not dispatch a second time.
        try:
            await service.decide(
                request_id,
                DecisionInput(
                    verdict=Verdict.ALLOW, expected_version=view.version, idempotency_key="one-call-allow-again"
                ),
                OPERATOR,
            )
            raise AssertionError("expected ActionConflictError on a stale/duplicate decision")
        except ActionConflictError:
            pass
        assert calls == [{"text": "hi"}]
    finally:
        await service.close()
        await executor.close()


async def test_tool_error_maps_to_safe_failed_result_without_leaking_tool_text(execution_lease: ExecutionLease) -> None:
    mcp = FastMCP("demo")

    @mcp.tool
    def explode() -> dict[str, str]:
        raise ToolError("credential xyz-secret-123 rejected by upstream")

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, mcp)
    await executor.start()
    try:
        result = await executor.execute(
            _request(action=ActionIdentity(group=GROUP_KEY, name="explode"), arguments={}), execution_lease
        )
        assert result.state is ExecutionState.FAILED
        assert result.error == {"kind": "mcp_tool_error", "message": "MCP tool reported an error"}
        assert "xyz-secret-123" not in str(result.error)
    finally:
        await executor.close()


async def test_execution_refuses_arguments_incompatible_with_the_current_live_schema(engine: AsyncEngine) -> None:
    """A valid submission waits for approval while the backend schema changes.

    Admission checks the advertised schema; execution must still fetch the current schema and
    refuse mismatched arguments without calling the tool, even when the mirror remains stale.
    """
    mcp = FastMCP("demo")
    calls: list[dict[str, Any]] = []

    @mcp.tool
    def foo(x: int) -> dict[str, int]:
        calls.append({"x": x})
        return {"x": x}

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, mcp)
    await executor.start()
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, ActionCatalog(groups={GROUP_KEY: group}), {GROUP_KEY: executor})
    try:
        await service.start()
        assert group.actions["foo"].input_schema["required"] == ["x"]

        submitted = await service.submit(
            ActionRequestInput(
                idempotency_key="schema-drift", action=ActionIdentity(group=GROUP_KEY, name="foo"), arguments={"x": 1}
            ),
            CALLER,
        )
        assert submitted.state is ActionState.DECISION_PENDING
        mcp.local_provider.remove_tool("foo")

        @mcp.tool(name="foo")
        def foo_v2(y: int) -> dict[str, int]:
            calls.append({"y": y})
            return {"y": y}

        # `arguments` still matches the OLD (mirrored) schema, not the new live one.
        await service.decide(
            submitted.id,
            DecisionInput(
                verdict=Verdict.ALLOW, expected_version=submitted.version, idempotency_key="schema-drift-allow"
            ),
            OPERATOR,
        )
        await _poll_state(store, submitted.id, want=ActionState.FAILED)
        result = (await store.get(submitted.id, CALLER)).execution
        assert result is not None
        assert result.state is ExecutionState.FAILED
        assert result.error == {
            "kind": "incompatible_action_schema",
            "message": "arguments no longer match the current tool schema",
        }
        assert calls == [], "the tool must never be called when arguments don't match the current schema"
    finally:
        await service.close()
        await executor.close()


async def test_ambiguous_transport_loss_becomes_execution_unknown_without_retry(engine: AsyncEngine) -> None:
    server_path = get_required_path(FAKE_SERVER)
    marker_path = Path(f"{__file__}.marker-{uuid4()}")
    await asyncio.to_thread(marker_path.unlink, missing_ok=True)

    group = ActionGroup(
        title="Slow demo group",
        description="Real subprocess MCP server for the transport-loss scenario.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="subprocess test server",
            config={
                "command": sys.executable,
                "args": [str(server_path)],
                "env": {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            },
        ),
    )
    executor = McpActionGroupExecutor.from_group("slow", group)
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store,
        ActionCatalog(groups={"slow": group}),
        {"slow": executor},
        lease_duration=timedelta(seconds=5),
        lease_sweep_interval=timedelta(seconds=1),
        executor_heartbeat_interval=timedelta(seconds=1),
        executor_health_timeout=timedelta(seconds=30),
    )
    await executor.start()
    await service.start()
    try:
        view = await service.submit(
            ActionRequestInput(
                idempotency_key="transport-loss",
                action=ActionIdentity(group="slow", name="slow_echo"),
                arguments={"marker_path": str(marker_path), "seconds": 30.0, "text": "hi"},
            ),
            CALLER,
        )
        await service.decide(
            view.id,
            DecisionInput(verdict=Verdict.ALLOW, expected_version=view.version, idempotency_key="transport-loss-allow"),
            OPERATOR,
        )

        for _ in range(500):
            if await asyncio.to_thread(marker_path.exists):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the tool never signalled it had started")

        # Sever the transport mid-call; the client's underlying subprocess dies with it.
        await executor.close()

        await _poll_state(store, view.id, want=ActionState.EXECUTION_UNKNOWN)
        final = await store.get(view.id, CALLER)
        assert final.execution is not None
        assert final.execution.error is not None
        assert final.execution.error["kind"] == "execution_outcome_unknown"
    finally:
        await asyncio.to_thread(marker_path.unlink, missing_ok=True)
        await service.close()


async def test_runtime_binds_configured_mcp_group(engine: AsyncEngine, tmp_path: Path) -> None:
    group = _group()
    group.executor.config = {
        "command": sys.executable,
        "args": [str(get_required_path(FAKE_SERVER))],
        "env": {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    }
    catalog = ActionCatalog(groups={"runtime": group})
    store = ActionStore(make_sessionmaker(engine))
    marker = tmp_path / "called"
    async with running_executor(catalog) as executors:
        assert set(catalog.groups["runtime"].actions) == {"slow_echo"}
        service = ActionService(store, catalog, executors)
        await service.start()
        try:
            view = await service.submit(
                ActionRequestInput(
                    idempotency_key="runtime",
                    action=ActionIdentity(group="runtime", name="slow_echo"),
                    arguments={"marker_path": str(marker), "seconds": 0, "text": "bound"},
                ),
                CALLER,
            )
            await service.decide(
                view.id,
                DecisionInput(verdict=Verdict.ALLOW, expected_version=view.version, idempotency_key="allow-runtime"),
                OPERATOR,
            )
            await _poll_state(store, view.id, want=ActionState.SUCCEEDED)
            result = await store.get(view.id, CALLER)
            assert result.execution is not None
            assert result.execution.result == {"echoed": "bound"}
            assert marker.read_text() == "started"
        finally:
            await service.close()


if __name__ == "__main__":
    pytest_bazel.main()
