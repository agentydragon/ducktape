"""Production wiring, live catalog routing, and startup/shutdown resource ownership."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import textwrap
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
import uvicorn
from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from util.bazel.runfiles import get_required_path
from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import DisabledOperatorAuthenticator
from x.agentplane.action_service.catalog import ActionCatalog, ActionGroup, ActionIdentity, McpExecutorBinding
from x.agentplane.action_service.db import ActionStore, make_sessionmaker
from x.agentplane.action_service.fixture_policy import FixtureAutoAllow
from x.agentplane.action_service.main import ActionServer, Settings, async_main
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
from x.agentplane.action_service.service import ActionService, UnsupportedActionError
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

CALLER = Principal(issuer="test", subject="sandbox", role=PrincipalRole.CALLER)
OPERATOR = Principal(issuer="test", subject="operator", role=PrincipalRole.OPERATOR)


def _group(config: dict[str, JsonValue]) -> ActionGroup:
    return ActionGroup(
        title="Reviewed backend",
        description="Composition test",
        executor=McpExecutorBinding(description="Test backend", config=config),
    )


def _request(action: ActionIdentity) -> ExecutionRequest:
    return ExecutionRequest(
        request_id=uuid4(), action=action, arguments={}, origin={}, correlation={}, caller_principal=CALLER.key
    )


async def test_empty_catalog_has_no_echo_fallback(engine: AsyncEngine) -> None:
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    catalog = ActionCatalog(groups=settings.action_groups)
    async with running_executor(catalog) as executors:
        assert executors == {}
        assert catalog.group_views() == []
        service = ActionService(ActionStore(make_sessionmaker(engine)), catalog, executors)
        for identity in (ActionIdentity(group="agentplane", name="echo"),):
            with pytest.raises(UnsupportedActionError):
                await service.submit(ActionRequestInput(idempotency_key="empty", action=identity, arguments={}), CALLER)


def test_missing_binding_is_rejected() -> None:
    with pytest.raises(ValidationError, match="executor"):
        ActionGroup.model_validate({"title": "Missing binding", "description": "Invalid"})


@pytest.mark.parametrize("config", [{}, {"command": ""}, {"command": 42}])
async def test_invalid_binding_fails_before_any_adapter_starts(config: dict[str, JsonValue]) -> None:
    catalog = ActionCatalog(
        groups={"first": _group({"transport": "stdio", "command": "unused"}), "invalid": _group(config)}
    )
    with patch.object(McpActionGroupExecutor, "start", new_callable=AsyncMock) as start:
        with pytest.raises(ValueError, match="ActionGroup 'invalid'"):
            async with running_executor(catalog):
                pytest.fail("invalid binding was served")
        start.assert_not_awaited()


@pytest.mark.parametrize("kind", ["echo", "hostexec", "unknown"])
def test_unsupported_executor_kind_is_rejected_by_settings(kind: str) -> None:
    with patch.object(sys, "argv", ["test_runtime"]), pytest.raises(ValidationError, match="union_tag_invalid"):
        Settings.model_validate(
            {
                "database_url": "postgresql+asyncpg://test.invalid/test",
                "action_groups": {
                    "invalid": {
                        "title": "Unsupported backend",
                        "description": "Must fail before runtime starts",
                        "executor": {"kind": kind, "description": "Test backend", "config": {"command": "unused"}},
                    }
                },
            }
        )


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_explicit_config_file_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_kind: str) -> None:
    path = tmp_path / "settings.yaml" if path_kind == "missing" else tmp_path
    monkeypatch.setenv("AGENTPLANE_ACTIONS_CONFIG_FILE", str(path))
    with pytest.raises(ValueError, match="configured Action Service settings file"):
        Settings(database_url="postgresql://unused", _cli_parse_args=False)


def test_config_file_loads_reviewed_group_and_rejects_malformed_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        textwrap.dedent("""
        action_groups:
          remote:
            title: Remote
            description: Reviewed peer
            executor:
              kind: mcp
              description: No credentials
              config:
                transport: streamable-http
                url: http://test-peer.invalid/mcp
                auth: none
    """)
    )
    monkeypatch.setenv("AGENTPLANE_ACTIONS_CONFIG_FILE", str(path))
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert settings.action_groups["remote"].executor.config == {
        "transport": "streamable-http",
        "url": "http://test-peer.invalid/mcp",
        "auth": "none",
    }
    path.write_text(
        path.read_text()
        .replace("kind: mcp", "kind: unsupported")
        .replace("http://test-peer.invalid/mcp", "http://test-peer.invalid/mcp?token=test-only-private")
    )
    with pytest.raises(ValidationError) as error:
        Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert "test-only-private" not in str(error.value)


def test_invalid_group_key_rejected_by_settings() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://unused", action_groups={"bad/key": _group({})}, _cli_parse_args=False)


async def test_runtime_sanitizes_connect_and_cleanup_failures() -> None:
    catalog = ActionCatalog(
        groups={
            "remote": _group({"transport": "streamable-http", "url": "http://test-peer.invalid/mcp", "auth": "none"})
        }
    )
    with (
        patch.object(McpActionGroupExecutor, "start", AsyncMock(side_effect=RuntimeError("private connect material"))),
        patch.object(McpActionGroupExecutor, "close", AsyncMock(side_effect=RuntimeError("private cleanup material"))),
        pytest.raises(RuntimeError, match="MCP shutdown failed") as error,
    ):
        async with running_executor(catalog):
            pytest.fail("unavailable runtime served")
    assert error.value.__suppress_context__


async def test_live_catalog_and_exact_group_dispatch(execution_lease: ExecutionLease) -> None:
    servers = {key: FastMCP(key) for key in ("one", "two")}
    for key, server in servers.items():
        # Same tool name on both servers proves routing uses the namespace, not the tool name.
        def owner(value: str = key) -> dict[str, str]:
            return {"owner": value}

        server.tool(name="owner")(owner)
    catalog = ActionCatalog(groups={key: _group({}) for key in servers})
    adapters = {key: McpActionGroupExecutor(key, catalog.groups[key], server) for key, server in servers.items()}
    with patch.object(McpActionGroupExecutor, "from_group", side_effect=lambda key, group: adapters[key]):
        async with running_executor(catalog) as executors:
            assert set(executors) == {"one", "two"}
            assert all(set(group.actions) == {"owner"} for group in catalog.groups.values())
            for key in servers:
                result = await executors[key].execute(
                    _request(ActionIdentity(group=key, name="owner")), execution_lease
                )
                assert result.state is ExecutionState.SUCCEEDED
                assert result.result == {"owner": key}
            assert (
                await executors["one"].execute(
                    _request(ActionIdentity(group="one_extra", name="owner")), execution_lease
                )
            ).state is ExecutionState.FAILED
            servers["one"].local_provider.remove_tool("owner")
            await adapters["one"].refresh_catalog()
            assert catalog.groups["one"].actions == {}


@pytest.mark.parametrize("failure", ["connect", "discovery", "cancel"])
async def test_partial_startup_closes_current_and_previous_adapter(failure: str) -> None:
    catalog = ActionCatalog(groups={key: _group({"transport": "stdio", "command": "unused"}) for key in ("one", "two")})
    events: list[str] = []
    adapters = {key: McpActionGroupExecutor.from_group(key, group) for key, group in catalog.groups.items()}
    names = {adapter: key for key, adapter in adapters.items()}

    async def start(adapter: McpActionGroupExecutor) -> None:
        events.append(f"start {names[adapter]}")
        if len(events) == 2:
            if failure == "connect":
                raise RuntimeError("connection failed")
            if failure == "cancel":
                raise asyncio.CancelledError
            catalog.groups["two"].available = False

    async def close(adapter: McpActionGroupExecutor) -> None:
        events.append(f"close {names[adapter]}")

    with (
        patch.object(McpActionGroupExecutor, "from_group", side_effect=lambda key, group: adapters[key]),
        patch.object(McpActionGroupExecutor, "start", start),
        patch.object(McpActionGroupExecutor, "close", close),
    ):
        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected):
            async with running_executor(catalog):
                pytest.fail("failed startup was served")
    assert events == ["start one", "start two", "close two", "close one"]


async def test_main_serves_real_stdio_execution_and_closes_in_order(db_url: str, tmp_path: Path) -> None:
    """Only Kubernetes configuration and the HTTP server loop are replaced; composition is real."""
    group = _group(
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(get_required_path("_main/x/agentplane/action_service/test_fixtures/fake_mcp_server.py"))],
            "env": {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        }
    )
    settings = Settings(database_url=db_url, action_groups={"demo": group}, _cli_parse_args=False)
    events: list[str] = []
    service_close = ActionService.close
    adapter_close = McpActionGroupExecutor.close

    async def close_service(service: ActionService) -> None:
        await service_close(service)
        events.append("service closed")

    async def close_adapter(adapter: McpActionGroupExecutor) -> None:
        await adapter_close(adapter)
        events.append("adapter closed")

    async def serve(server: uvicorn.Server) -> None:
        app = cast(FastAPI, server.config.app)
        service = cast(ActionService, app.state.action_service)
        catalog = cast(ActionCatalog, app.state.action_catalog)
        assert catalog.groups["demo"] is group
        assert set(group.actions) == {"slow_echo"}
        with pytest.raises(UnsupportedActionError):
            await service.submit(
                ActionRequestInput(
                    idempotency_key="no-echo", action=ActionIdentity(group="agentplane", name="echo"), arguments={}
                ),
                CALLER,
            )
        view = await service.submit(
            ActionRequestInput(
                idempotency_key="real-stdio",
                action=ActionIdentity(group="demo", name="slow_echo"),
                arguments={"marker_path": str(tmp_path / "called"), "seconds": 0, "text": "wired"},
            ),
            CALLER,
        )
        await service.decide(
            view.id,
            DecisionInput(verdict=Verdict.ALLOW, expected_version=view.version, idempotency_key="allow"),
            OPERATOR,
        )
        async with asyncio.timeout(10):
            for _ in range(1000):
                view = await service.get(view.id, CALLER)
                if view.state is ActionState.SUCCEEDED:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("stdio execution did not succeed")
        assert view.execution is not None
        assert view.execution.result == {"echoed": "wired"}
        assert await asyncio.to_thread((tmp_path / "called").read_text) == "started"
        events.append("executed")

    with (
        patch("x.agentplane.action_service.main.k8s_config.load_incluster_config"),
        patch.object(uvicorn.Server, "serve", serve),
        patch.object(ActionService, "close", close_service),
        patch.object(McpActionGroupExecutor, "close", close_adapter),
    ):
        await async_main(settings)
    assert events == ["executed", "service closed", "adapter closed"]
    assert not any(task.get_name().startswith("mcp-executor-refresh-") for task in asyncio.all_tasks())


@pytest.mark.parametrize("failure", ["schema", "service_start", "serve", "cancel"])
async def test_main_failure_disposes_engine_after_owned_resources(failure: str) -> None:
    events: list[str] = []
    engine = MagicMock(spec=AsyncEngine)

    async def dispose() -> None:
        events.append("engine disposed")

    async def verify(unused: AsyncEngine) -> None:
        if failure == "schema":
            raise RuntimeError("schema failed")

    async def start(service: ActionService) -> None:
        events.append("service started")
        if failure == "service_start":
            raise RuntimeError("startup failed")

    async def close(service: ActionService) -> None:
        events.append("service closed")

    async def serve(server: uvicorn.Server) -> None:
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("serve failed")

    engine.dispose = AsyncMock(side_effect=dispose)
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    with (
        patch("x.agentplane.action_service.main.make_engine", return_value=engine),
        patch("x.agentplane.action_service.main.verify_schema", verify),
        patch("x.agentplane.action_service.main.k8s_config.load_incluster_config"),
        patch.object(ActionService, "start", start),
        patch.object(ActionService, "close", close),
        patch.object(uvicorn.Server, "serve", serve),
    ):
        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected):
            await async_main(settings)
    assert events == (
        ["engine disposed"] if failure == "schema" else ["service started", "service closed", "engine disposed"]
    )
    engine.dispose.assert_awaited_once()


async def test_main_auto_allows_upstream_everything(db_url: str, everything_url: str) -> None:
    """Real production composition + fixture HTTP; only Kubernetes setup and uvicorn loop are replaced."""
    caller = Principal(issuer="kubernetes-sandbox", subject="agentplane-staging:fixture-uid", role=PrincipalRole.CALLER)
    settings = Settings(
        database_url=db_url,
        action_groups={"fixture": _group({"transport": "streamable-http", "url": everything_url, "auth": "none"})},
        fixture_auto_allow=FixtureAutoAllow(group="fixture"),
        _cli_parse_args=False,
    )

    async def serve(server: uvicorn.Server) -> None:
        app = cast(FastAPI, server.config.app)
        service = cast(ActionService, app.state.action_service)
        catalog = cast(ActionCatalog, app.state.action_catalog)
        assert "echo" in catalog.groups["fixture"].actions
        body = ActionRequestInput(
            idempotency_key="fixture-once",
            action=ActionIdentity(group="fixture", name="echo"),
            arguments={"message": "MCP0-ok"},
        )
        view = await service.submit(body, caller)
        assert view.decision is not None
        assert view.decision.provider == "mcp_fixture"
        async with asyncio.timeout(10):
            while view.state is not ActionState.SUCCEEDED:
                await asyncio.sleep(0.01)
                view = await service.get(view.id, caller)
        assert view.execution is not None
        assert view.execution.result == {"content": ["Echo: MCP0-ok"]}
        assert (await service.submit(body, caller)).execution == view.execution
        # Agent claims cannot opt an untrusted caller into the fixture policy.
        pending = await service.submit(
            ActionRequestInput(
                idempotency_key="untrusted",
                action=body.action,
                arguments={"message": "MCP0-ok"},
                origin={"caller": caller.key},
            ),
            CALLER,
        )
        assert pending.state is ActionState.DECISION_PENDING
        assert pending.execution is None

    with (
        patch("x.agentplane.action_service.main.k8s_config.load_incluster_config"),
        patch.object(uvicorn.Server, "serve", serve),
    ):
        await async_main(settings)


async def test_sigterm_fences_readiness_and_traffic_before_http_shutdown(engine: AsyncEngine, db_url: str) -> None:
    catalog = ActionCatalog(groups={})
    service = ActionService(ActionStore(make_sessionmaker(engine)), catalog, {})
    app = create_app(
        service,
        MagicMock(spec=SandboxPrincipalAuthenticator),
        DisabledOperatorAuthenticator(),
        catalog,
        updates=ActionUpdates(db_url),
    )
    server = ActionServer(uvicorn.Config(app, timeout_graceful_shutdown=5), service)
    original_handler = signal.getsignal(signal.SIGTERM)
    # Uvicorn re-raises captured signals to the previous handler on context exit.
    signal.signal(signal.SIGTERM, lambda sig, frame: None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test-actions") as client:
            assert (await client.get("/readyz")).status_code == 200
            with server.capture_signals():
                signal.raise_signal(signal.SIGTERM)
                assert server.should_exit
                assert service.draining
                assert (await client.get("/readyz")).status_code == 503
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.post("/v1/action-requests", json={})).status_code == 503
                assert (await client.post("/mcp", json={})).status_code == 503
        await service.close()
    finally:
        signal.signal(signal.SIGTERM, original_handler)


if __name__ == "__main__":
    pytest_bazel.main()
