"""Pinned FastMCP/MCP client against a deterministic streamable-HTTP peer on a real socket.

Both JSON and finite SSE replies exercise the production from_group transport selection. The
peer records whole JSON-RPC payloads, and can fail after accepting a call without relying on timing.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
import uvicorn
from fastapi import FastAPI
from httpx import HTTPStatusError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from util.testing.asgi import serve_app_sync
from x.agentplane.action_service.catalog import ActionCatalog, ActionGroup, ActionIdentity, McpExecutorBinding
from x.agentplane.action_service.db import ExecutionRow, make_sessionmaker
from x.agentplane.action_service.main import Settings, async_main
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor, _LinkageBearerAuth
from x.agentplane.action_service.mcp_linkage import McpLinkageStatus
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


class FakeMcpServer:
    def __init__(self, *, sse: bool) -> None:
        self.sse = sse
        self.requests: list[Request] = []
        self.posts: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = [
            {
                "name": "echo",
                "description": "Echo text over HTTP.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ]
        self.list_unavailable = False
        self.call_unavailable = False
        self.tool_error = False

    async def handle(self, request: Request) -> Response:
        self.requests.append(request)
        if request.method == "GET":
            return Response(status_code=405)
        if request.method == "DELETE":
            return Response(status_code=204)
        body = await request.json()
        self.posts.append(body)
        method = body["method"]
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": body["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "test-http-peer", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "test-http-session"},
            )
        result: dict[str, Any]
        if method == "tools/list":
            if self.list_unavailable:
                return Response(status_code=503)
            result = {"tools": self.tools}
        elif method == "tools/call":
            if self.call_unavailable:
                # The peer accepted the call; a missing result cannot prove it did not execute.
                return Response(status_code=503)
            result = {
                "content": [{"type": "text", "text": "test-only private backend error" if self.tool_error else "hi"}],
                "isError": self.tool_error,
            }
            if not self.tool_error:
                result["structuredContent"] = {
                    "echoed": body["params"]["arguments"]["text"],
                    "api_key": "test-only-backend-secret",
                }
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        response = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            return Response(f"event: message\ndata: {json.dumps(response)}\n\n", media_type="text/event-stream")
        return JSONResponse(response)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return [post for post in self.posts if post["method"] == "tools/call"]


@pytest.fixture(params=[False, True], ids=["json", "sse"])
def fake_server(request: pytest.FixtureRequest) -> FakeMcpServer:
    return FakeMcpServer(sse=request.param)


@pytest.fixture
def http_group(fake_server: FakeMcpServer) -> Iterator[ActionGroup]:
    app = Starlette(routes=[Route("/test-mcp", fake_server.handle, methods=["POST", "GET", "DELETE"])])
    with serve_app_sync(app) as url:
        yield ActionGroup(
            title="HTTP test group",
            description="Credentialless test peer",
            executor=McpExecutorBinding(
                kind="mcp",
                description="HTTP test peer",
                config={"transport": "streamable-http", "url": f"{url}/test-mcp", "auth": "none"},
            ),
        )


@pytest.fixture
async def executor(http_group: ActionGroup, fake_server: FakeMcpServer) -> AsyncIterator[McpActionGroupExecutor]:
    executor = McpActionGroupExecutor.from_group("remote", http_group, catalog_refresh_interval=timedelta(hours=1))
    try:
        await executor.start()
        yield executor
    finally:
        if fake_server.list_unavailable or fake_server.call_unavailable:
            # The pinned client re-raises its terminal HTTP failure when joining the session task.
            with pytest.raises(HTTPStatusError, match="503 Service Unavailable"):
                await executor.close()
        else:
            await executor.close()


@pytest.fixture
def execution_request() -> ExecutionRequest:
    return ExecutionRequest(
        request_id=uuid4(),
        action=ActionIdentity(group="remote", name="echo"),
        arguments={"text": "hi"},
        origin={},
        correlation={},
        caller_principal="test-http-caller",
    )


async def test_http_session_discovery_call_and_shutdown(
    execution_lease: ExecutionLease,
    http_group: ActionGroup,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    executor = McpActionGroupExecutor.from_group("remote", http_group)
    try:
        await executor.start()
        assert http_group.available
        assert set(http_group.actions) == {"echo"}
        assert http_group.actions["echo"].description == "Echo text over HTTP."
        assert http_group.actions["echo"].input_schema == fake_server.tools[0]["inputSchema"]
        result = await executor.execute(execution_request, execution_lease)
        assert result.state is ExecutionState.SUCCEEDED
        assert result.result == {"echoed": "hi", "api_key": "test-only-backend-secret"}
        assert [post["method"] for post in fake_server.posts] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/list",
            "tools/call",
        ]
        assert len(fake_server.calls) == 1
        assert fake_server.calls[0]["params"]["name"] == "echo"
        assert fake_server.calls[0]["params"]["arguments"] == {"text": "hi"}
    finally:
        await executor.close()
    assert fake_server.requests[-1].method == "DELETE"
    assert all("authorization" not in request.headers for request in fake_server.requests)
    for request in fake_server.requests[1:]:
        assert request.headers["mcp-session-id"] == "test-http-session"
        assert request.headers["mcp-protocol-version"]


async def test_http_revalidates_live_schema_before_dispatch(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.tools[0]["inputSchema"]["required"] = ["other"]
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "incompatible_action_schema"
    assert fake_server.calls == []


async def test_http_refresh_and_unavailable_catalog(
    executor: McpActionGroupExecutor, http_group: ActionGroup, fake_server: FakeMcpServer
) -> None:
    fake_server.tools = []
    await executor.refresh_catalog()
    assert http_group.available
    assert http_group.actions == {}
    fake_server.list_unavailable = True
    await executor.refresh_catalog()
    assert not http_group.available
    assert http_group.actions == {}


async def test_http_list_failure_refuses_dispatch(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.list_unavailable = True
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "mcp_unavailable"
    assert fake_server.calls == []


async def test_http_tool_error_is_safe_failure(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.tool_error = True
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert result.error == {"kind": "mcp_tool_error", "message": "MCP tool reported an error"}
    assert len(fake_server.calls) == 1


async def test_http_missing_call_result_is_unknown_without_retry(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.call_unavailable = True
    with pytest.raises(ExecutionOutcomeUnknownError, match="MCP tools/call transport failure"):
        await executor.execute(execution_request, execution_lease)
    assert len(fake_server.calls) == 1


async def test_unlinked_oauth_group_starts_dormant_without_connecting() -> None:
    group = ActionGroup(
        title="OAuth test group",
        description="Unlinked OAuth test peer",
        executor=McpExecutorBinding(
            kind="mcp",
            description="OAuth test peer",
            config={
                "transport": "streamable-http",
                "url": "https://unlinked.invalid/mcp",
                "auth": "oauth",
                "server_id": "github",
            },
        ),
    )
    linkage = AsyncMock()
    linkage_changed = asyncio.Event()
    linkage.subscribe_changes = Mock(return_value=linkage_changed)
    linkage.unsubscribe_changes = Mock()
    linkage.status.side_effect = [
        SimpleNamespace(status=McpLinkageStatus.UNLINKED),
        SimpleNamespace(status=McpLinkageStatus.LINKED),
    ]
    with patch("x.agentplane.action_service.mcp_executor.StreamableHttpTransport") as transport:
        executor = McpActionGroupExecutor.from_group_with_linkage("remote", group, linkage)
        connected = asyncio.Event()
        release = asyncio.Event()

        async def connect() -> None:
            connected.set()
            await release.wait()

        with patch.object(executor, "_connect_client", connect):
            await executor.start()
            try:
                await asyncio.sleep(0)
                assert not group.available
                assert group.actions == {}
                assert transport.call_count == 0
                linkage.status.assert_awaited()
                linkage_changed.set()
                async with asyncio.timeout(1):
                    await connected.wait()
            finally:
                await executor.close()


async def test_oauth_auth_resolves_the_current_token_for_each_request() -> None:
    linkage = AsyncMock()
    linkage.access_token_for_execution.side_effect = ["token-one", "token-two"]
    auth = _LinkageBearerAuth(linkage, "github")

    async def authenticated(request: httpx.Request) -> httpx.Request:
        async for result in auth.async_auth_flow(request):
            return cast(httpx.Request, result)
        raise AssertionError("auth flow did not yield a request")

    first = await authenticated(httpx.Request("GET", "https://test.invalid/mcp"))
    second = await authenticated(httpx.Request("GET", "https://test.invalid/mcp"))
    assert first.headers["Authorization"] == "Bearer token-one"
    assert second.headers["Authorization"] == "Bearer token-two"


@pytest.mark.parametrize(
    "config",
    [
        {"transport": "streamable-http", "url": "file:///tmp/test-mcp"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp?token=test-only-secret"},
        {"transport": "streamable-http", "url": "http://test-user@test.invalid/mcp"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp#fragment"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "command": "test-server"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "headers": {}},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "auth": "oauth"},
        {"transport": "sse", "url": "https://test.invalid/mcp"},
    ],
)
def test_unsupported_http_config_fails_before_connecting(config: dict[str, Any]) -> None:
    group = ActionGroup(
        title="Invalid HTTP test group",
        description="No connection should be attempted",
        executor=McpExecutorBinding(kind="mcp", description="Invalid test peer", config=config),
    )
    with pytest.raises(ValidationError):
        McpActionGroupExecutor.from_group("remote", group)


@pytest.mark.parametrize("invalid", ["schema", "duplicate"])
async def test_invalid_discovery_clears_stale_actions(
    executor: McpActionGroupExecutor, http_group: ActionGroup, fake_server: FakeMcpServer, invalid: str
) -> None:
    assert "echo" in http_group.actions
    if invalid == "schema":
        fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
    else:
        fake_server.tools.append(fake_server.tools[0])
    await executor.refresh_catalog()
    assert not http_group.available
    assert http_group.actions == {}
    assert fake_server.calls == []


async def test_invalid_live_schema_refuses_before_call(
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
    execution_lease: ExecutionLease,
) -> None:
    fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
    result = await executor.execute(execution_request, execution_lease)
    assert result.error == {"kind": "mcp_invalid_schema", "message": "backend tool schema is invalid"}
    assert fake_server.calls == []


@pytest.mark.parametrize("failure", ["unavailable", "invalid_schema"])
async def test_runtime_rejects_remote_discovery_without_leaking_transport(
    http_group: ActionGroup, fake_server: FakeMcpServer, failure: str
) -> None:
    if failure == "unavailable":
        fake_server.list_unavailable = True
    else:
        fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
    with pytest.raises(RuntimeError) as error:
        async with running_executor(ActionCatalog(groups={"remote": http_group})):
            pytest.fail("unavailable remote was served")
    rendered = "".join(traceback.format_exception(error.value))
    assert "ActionGroup 'remote'" in rendered
    assert str(http_group.executor.config["url"]) not in rendered
    assert "HTTPStatusError" not in rendered
    assert not http_group.available
    assert http_group.actions == {}
    assert fake_server.calls == []


@pytest.mark.parametrize("outcome", ["success", "tool_error", "unknown", "schema_mismatch", "invalid_schema"])
async def test_production_http_composition_one_execution_no_replay(
    db_url: str, engine: AsyncEngine, http_group: ActionGroup, fake_server: FakeMcpServer, outcome: str
) -> None:
    """Real main, HTTP MCP, and PostgreSQL; only Kubernetes setup and the serving loop are replaced."""
    caller = Principal(issuer="test-http", subject="sandbox", role=PrincipalRole.CALLER)
    operator = Principal(issuer="test-http", subject="operator", role=PrincipalRole.OPERATOR)
    settings = Settings(database_url=db_url, action_groups={"remote": http_group}, _cli_parse_args=False)

    async def serve(server: uvicorn.Server) -> None:
        app = cast(FastAPI, server.config.app)
        service = cast(ActionService, app.state.action_service)
        catalog = cast(ActionCatalog, app.state.action_catalog)
        assert catalog.action_view("remote", "echo").input_schema == fake_server.tools[0]["inputSchema"]
        assert str(http_group.executor.config["url"]) not in "".join(
            view.model_dump_json() for view in catalog.group_views()
        )
        body = ActionRequestInput(
            idempotency_key="http-once", action=ActionIdentity(group="remote", name="echo"), arguments={"text": "hi"}
        )
        pending = await service.submit(body, caller)
        assert pending.state is ActionState.DECISION_PENDING
        assert fake_server.calls == []
        fake_server.tool_error = outcome == "tool_error"
        fake_server.call_unavailable = outcome == "unknown"
        if outcome == "schema_mismatch":
            fake_server.tools[0]["inputSchema"]["required"] = ["other"]
        if outcome == "invalid_schema":
            fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
        decision = DecisionInput(verdict=Verdict.ALLOW, expected_version=pending.version, idempotency_key="allow-once")
        await service.decide(pending.id, decision, operator)
        expected = {
            "success": ActionState.SUCCEEDED,
            "tool_error": ActionState.FAILED,
            "unknown": ActionState.EXECUTION_UNKNOWN,
            "schema_mismatch": ActionState.FAILED,
            "invalid_schema": ActionState.FAILED,
        }[outcome]
        async with asyncio.timeout(10):
            while (final := await service.get(pending.id, caller)).state is not expected:
                pass  # Each database read yields; wait for durable completion, not an elapsed delay.
        assert final.execution is not None
        assert final.execution.result == ({"echoed": "hi", "api_key": "[redacted]"} if outcome == "success" else None)
        if outcome != "success":
            assert final.execution.error is not None
            assert (
                final.execution.error["kind"]
                == {
                    "tool_error": "mcp_tool_error",
                    "unknown": "execution_outcome_unknown",
                    "schema_mismatch": "incompatible_action_schema",
                    "invalid_schema": "mcp_invalid_schema",
                }[outcome]
            )
        for principal in (caller, operator):
            view = await service.get(pending.id, principal)
            assert "test-only" not in view.model_dump_json()
            assert str(http_group.executor.config["url"]) not in view.model_dump_json()
        assert (await service.submit(body, caller)).execution == final.execution
        assert (await service.decide(pending.id, decision, operator)).execution == final.execution
        # Duplicate dispatch and restart recovery must both respect the durable single-Execution claim.
        await service._dispatch_once(pending.id)
        await service.close()
        await service.start()
        assert (await service.get(pending.id, caller)).execution == final.execution
        assert len(fake_server.calls) == (0 if outcome in {"schema_mismatch", "invalid_schema"} else 1)
        async with make_sessionmaker(engine)() as session:
            assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1
        if fake_server.calls:
            assert fake_server.calls[0]["params"]["arguments"] == {"text": "hi"}

    with (
        patch("x.agentplane.action_service.main.k8s_config.load_incluster_config"),
        patch.object(uvicorn.Server, "serve", serve),
    ):
        if outcome == "unknown":
            # Closing the pinned client re-raises its terminal HTTP failure, safely wrapped by composition.
            with pytest.raises(RuntimeError, match="MCP shutdown failed"):
                await async_main(settings)
        else:
            await async_main(settings)
    assert not any(task.get_name().startswith("mcp-executor-refresh-") for task in asyncio.all_tasks())


if __name__ == "__main__":
    pytest_bazel.main()
