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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
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
from x.agentplane.action_service.service import ActionService, ExecutionOutcomeUnknownError
from x.agentplane.action_service.test_fixtures.lifecycle import wait_available

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
    await wait_available(executor._group)
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
    await wait_available(executor._group)
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
    await wait_available(executor._group)
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
    await wait_available(executor._group)
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


async def test_tool_error_output_is_a_successful_result(execution_lease: ExecutionLease) -> None:
    mcp = FastMCP("demo")

    @mcp.tool
    def explode() -> dict[str, str]:
        raise ToolError("credential xyz-secret-123 rejected by upstream")

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, mcp)
    await executor.start()
    await wait_available(executor._group)
    try:
        result = await executor.execute(
            _request(action=ActionIdentity(group=GROUP_KEY, name="explode"), arguments={}), execution_lease
        )
        assert result.state is ExecutionState.SUCCEEDED
        assert result.error is None
        assert result.result == {"is_error": True, "content": ["credential xyz-secret-123 rejected by upstream"]}
        assert group.available
        assert executor._connection is not None
        assert executor._connection.client.is_connected()
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
    await wait_available(executor._group)
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
                "transport": "stdio",
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
    await wait_available(executor._group)
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
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(get_required_path(FAKE_SERVER))],
        "env": {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    }
    catalog = ActionCatalog(groups={"runtime": group})
    store = ActionStore(make_sessionmaker(engine))
    marker = tmp_path / "called"
    async with running_executor(catalog) as executors:
        await wait_available(group)
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


class ControlledLease:
    renewal_interval = timedelta(milliseconds=10)

    def __init__(self, started: asyncio.Event, failure: str) -> None:
        self.started = started
        self.failure = failure
        self.renewed = asyncio.Event()
        self.calls = 0
        self.active = 0

    async def heartbeat(self) -> bool:
        self.active += 1
        self.calls += 1
        try:
            if self.started.is_set():
                self.renewed.set()
                if self.failure == "lost":
                    return False
                if self.failure == "database":
                    raise RuntimeError("test-private-database-detail")
                if self.failure == "hung":
                    await asyncio.Event().wait()
            return True
        finally:
            self.active -= 1


@pytest.mark.parametrize("failure", ["lost", "database", "hung"])
async def test_mcp_renewal_failure_stops_waiting_without_retry(failure: str) -> None:
    server = FastMCP("lease-test")
    started = asyncio.Event()
    calls = []

    @server.tool
    async def blocked() -> str:
        calls.append("called")
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    lease = ControlledLease(started, failure)
    executor = McpActionGroupExecutor(GROUP_KEY, _group(), server)
    await executor.start()
    await wait_available(executor._group)
    try:
        async with asyncio.timeout(5):
            with pytest.raises(ExecutionOutcomeUnknownError, match="lease") as error:
                await executor.execute(
                    _request(action=ActionIdentity(group=GROUP_KEY, name="blocked"), arguments={}), lease
                )
        assert "test-private" not in str(error.value)
        assert calls == ["called"]
        assert lease.active == 0
        assert not any(task.get_name() == "mcp-execution-renewal" for task in asyncio.all_tasks())
    finally:
        await executor.close()


async def test_discovery_wait_renews_and_lost_lease_prevents_tool_call() -> None:
    server = FastMCP("discovery-lease-test")
    listing, release_list = asyncio.Event(), asyncio.Event()
    calls = []

    @server.tool
    def echo() -> str:
        calls.append("called")
        return "echo"

    lease = ControlledLease(listing, "healthy")
    executor = McpActionGroupExecutor(GROUP_KEY, _group(), server)
    await executor.start()
    await wait_available(executor._group)
    assert executor._connection is not None
    client = executor._connection.client
    list_tools = client.list_tools

    async def gated_list():
        listing.set()
        await release_list.wait()
        return await list_tools()

    try:
        with patch.object(client, "list_tools", gated_list):
            task = asyncio.create_task(
                executor.execute(_request(action=ActionIdentity(group=GROUP_KEY, name="echo"), arguments={}), lease)
            )
            async with asyncio.timeout(5):
                await lease.renewed.wait()
                lease.failure = "lost"
                release_list.set()
                with pytest.raises(ExecutionOutcomeUnknownError, match="lease"):
                    await task
        assert calls == []
        assert lease.active == 0
        assert not any(task.get_name() == "mcp-execution-renewal" for task in asyncio.all_tasks())
    finally:
        release_list.set()
        await executor.close()


async def test_mcp_cancellation_joins_renewal_task() -> None:
    server = FastMCP("cancel-test")
    started = asyncio.Event()

    @server.tool
    async def blocked() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    lease = ControlledLease(started, "healthy")
    executor = McpActionGroupExecutor(GROUP_KEY, _group(), server)
    await executor.start()
    await wait_available(executor._group)
    try:
        task = asyncio.create_task(
            executor.execute(_request(action=ActionIdentity(group=GROUP_KEY, name="blocked"), arguments={}), lease)
        )
        async with asyncio.timeout(5):
            await lease.renewed.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert lease.active == 0
        assert not any(task.get_name() == "mcp-execution-renewal" for task in asyncio.all_tasks())
    finally:
        await executor.close()


async def test_mcp_deadline_stops_healthy_renewal(execution_lease: ExecutionLease) -> None:
    server = FastMCP("deadline-test")

    @server.tool
    async def blocked() -> str:
        await asyncio.Event().wait()
        return "unreachable"

    executor = McpActionGroupExecutor(GROUP_KEY, _group(), server, execution_timeout=timedelta(milliseconds=30))
    await executor.start()
    await wait_available(executor._group)
    try:
        async with asyncio.timeout(5):
            with pytest.raises(ExecutionOutcomeUnknownError, match="deadline"):
                await executor.execute(
                    _request(action=ActionIdentity(group=GROUP_KEY, name="blocked"), arguments={}), execution_lease
                )
        assert not any(task.get_name() == "mcp-execution-renewal" for task in asyncio.all_tasks())
    finally:
        await executor.close()


async def test_long_mcp_call_drains_with_live_execution_and_executor_leases(engine: AsyncEngine) -> None:
    server = FastMCP("drain-test")
    started, release, renewed_past_window, process_heartbeat = (asyncio.Event() for _ in range(4))
    calls = []

    @server.tool
    async def echo_once(text: str) -> dict[str, str]:
        calls.append(text)
        started.set()
        await release.wait()
        return {"echoed": text}

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, server)
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store,
        ActionCatalog(groups={GROUP_KEY: group}),
        {GROUP_KEY: executor},
        on_drain=executor.begin_drain,
        lease_duration=timedelta(milliseconds=300),
        executor_heartbeat_interval=timedelta(milliseconds=30),
    )
    heartbeat = store.heartbeat_execution
    record = store.record_executor_heartbeat
    original_deadline: datetime | None = None

    async def renew(*args, **kwargs):
        nonlocal original_deadline
        result = await heartbeat(*args, **kwargs)
        if original_deadline is None:
            original_deadline = datetime.now(UTC) + kwargs["lease_duration"]
        elif datetime.now(UTC) > original_deadline and service.draining:
            renewed_past_window.set()
        return result

    async def record_process(*args, **kwargs):
        await record(*args, **kwargs)
        if service.draining:
            process_heartbeat.set()

    await executor.start()
    await wait_available(executor._group)
    try:
        with (
            patch.object(store, "heartbeat_execution", renew),
            patch.object(store, "record_executor_heartbeat", record_process),
        ):
            await service.start()
            request_id = await _allowed_execution(service, idempotency_key="long-drain")
            async with asyncio.timeout(5):
                await started.wait()
                service.begin_drain()
                closing = asyncio.create_task(service.close())
                await renewed_past_window.wait()
                await process_heartbeat.wait()
                assert await store.expire_stale_leases(executor_health_timeout=timedelta(seconds=1)) == []
                assert (await store.get(request_id, CALLER)).state is ActionState.RUNNING
                assert not closing.done()
                release.set()
                await closing
            final = await store.get(request_id, CALLER)
            assert final.state is ActionState.SUCCEEDED
            assert final.execution is not None
            assert final.execution.result == {"echoed": "hi"}
            assert calls == ["hi"]
    finally:
        release.set()
        await service.close()
        await executor.close()


@pytest.mark.parametrize("remove_action", [False, True])
async def test_approved_work_stays_unclaimed_during_outage_then_recovers(
    engine: AsyncEngine, remove_action: bool
) -> None:
    server = FastMCP("outage")
    calls: list[str] = []

    @server.tool
    def echo_once(text: str) -> str:
        calls.append(text)
        return text

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, server)
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store,
        ActionCatalog(groups={GROUP_KEY: group}),
        {GROUP_KEY: executor},
        dispatch_poll_interval=timedelta(milliseconds=10),
    )
    await executor.start()
    await wait_available(group)
    try:
        view = await service.submit(
            ActionRequestInput(
                idempotency_key="outage",
                action=ActionIdentity(group=GROUP_KEY, name="echo_once"),
                arguments={"text": "once"},
            ),
            CALLER,
        )
        executor._mark_unavailable()
        allowed, _ = await store.decide(
            view.id,
            DecisionInput(verdict=Verdict.ALLOW, expected_version=view.version, idempotency_key="allow-outage"),
            OPERATOR,
            provider="human",
        )
        for _ in range(3):
            await service._dispatch_once(view.id)
        waiting = await store.get(view.id, CALLER)
        assert waiting.execution is not None
        assert waiting.execution.state is ExecutionState.PENDING_DISPATCH
        assert waiting.execution.started_at is None
        assert waiting.version == allowed.version
        assert waiting.decision == allowed.decision
        assert calls == []
        if remove_action:
            server.local_provider.remove_tool("echo_once")
        await executor.refresh_catalog()
        await service.start()
        await _poll_state(store, view.id, want=ActionState.FAILED if remove_action else ActionState.SUCCEEDED)
        assert calls == ([] if remove_action else ["once"])
    finally:
        await service.close()
        await executor.close()


async def test_failed_discovery_reconnects_without_tearing_down_an_active_call(execution_lease: ExecutionLease) -> None:
    server = FastMCP("retirement")
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    @server.tool
    async def gated() -> str:
        calls.append("called")
        started.set()
        await release.wait()
        return "completed"

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, server)
    await executor.start()
    await wait_available(group)
    old = executor._connection
    assert old is not None
    task = asyncio.create_task(
        executor.execute(_request(action=ActionIdentity(group=GROUP_KEY, name="gated"), arguments={}), execution_lease)
    )
    try:
        async with asyncio.timeout(5):
            await started.wait()
        with patch.object(old.client, "list_tools", side_effect=OSError("test-only transport failure")):
            await executor.refresh_catalog()
            assert not group.available
            await wait_available(group)
        assert executor._connection is not old
        assert old.client.is_connected()
        assert not task.done()
        release.set()
        assert (await task).state is ExecutionState.SUCCEEDED
        assert calls == ["called"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await executor.close()
    assert not old.client.is_connected()
    assert not any(task.get_name().startswith(("mcp-wakeup-", "mcp-retire-")) for task in asyncio.all_tasks())


async def test_execution_never_switches_generation_after_schema_check(execution_lease: ExecutionLease) -> None:
    server = FastMCP("generation")
    listing, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    @server.tool
    def echo() -> str:
        calls.append("called")
        return "echo"

    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, server)
    await executor.start()
    await wait_available(group)
    old = executor._connection
    assert old is not None
    list_tools = old.client.list_tools

    async def list_then_fail():
        if listing.is_set():
            raise OSError("test-only disconnected generation")
        tools = await list_tools()
        listing.set()
        await release.wait()
        return tools

    try:
        with patch.object(old.client, "list_tools", list_then_fail):
            task = asyncio.create_task(
                executor.execute(
                    _request(action=ActionIdentity(group=GROUP_KEY, name="echo"), arguments={}), execution_lease
                )
            )
            async with asyncio.timeout(5):
                await listing.wait()
                await executor.refresh_catalog()
                await wait_available(group)
                assert executor._connection is not old
                release.set()
                result = await task
        assert result.state is ExecutionState.FAILED
        assert result.error is not None
        assert result.error["kind"] == "mcp_unavailable"
        assert calls == []
    finally:
        release.set()
        await executor.close()


async def test_supervisor_death_revokes_availability_and_joins_wakeup_tasks() -> None:
    group = _group()
    executor = McpActionGroupExecutor(GROUP_KEY, group, FastMCP("supervisor"))
    await executor.start()
    await wait_available(group)
    assert executor._supervisor is not None
    executor._supervisor.cancel()
    await asyncio.gather(executor._supervisor, return_exceptions=True)
    assert not group.available
    assert group.health is not None
    assert group.health.reason == "supervisor_stopped"
    await executor.close()
    assert not any(task.get_name().startswith("mcp-wakeup-") for task in asyncio.all_tasks())


if __name__ == "__main__":
    pytest_bazel.main()
