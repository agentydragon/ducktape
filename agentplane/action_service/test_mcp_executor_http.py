"""Pinned FastMCP/MCP client against a deterministic streamable-HTTP peer on a real socket.

The peer replies in JSON; the tests that read a success reply back also run against finite SSE
replies (`REPLY_ENCODINGS`), since the reply encoding only matters there. The peer records whole
JSON-RPC payloads, and can fail after accepting a call without relying on timing.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentplane.action_service.catalog import ActionCatalog, ActionGroup, ActionIdentity, McpExecutorBinding
from agentplane.action_service.mcp_executor import McpActionGroupExecutor, _LinkageBearerAuth
from agentplane.action_service.mcp_linkage import McpLinkageStatus
from agentplane.action_service.models import ExecutionRequest, ExecutionState
from agentplane.action_service.runtime import running_executor
from agentplane.action_service.service import ExecutionLease, ExecutionOutcomeUnknownError
from agentplane.action_service.testing.lifecycle import wait_available, wait_retry
from agentplane.subjects import ServiceAccountRef
from util.testing.asgi import serve_app_sync


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
        self.list_error_body = b""
        self.list_error_headers: dict[str, str] = {}
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
        if method == "server/discover":
            # This peer only speaks the legacy initialize handshake. A real legacy server
            # answers an unrecognized method with a JSON-RPC error, not a transport failure --
            # that's what lets the client's mode="auto" probe fall back to initialize() instead
            # of treating the peer as broken (see mcp.client._probe.negotiate_auto).
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not found"}}
            )
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
                return Response(self.list_error_body, status_code=503, headers=self.list_error_headers)
            result = {"tools": self.tools}
        elif method == "tools/call":
            if self.call_unavailable:
                # The peer accepted the call; a missing result cannot prove it did not execute.
                return Response(status_code=503)
            result = {
                "content": [{"type": "text", "text": "backend tool error text" if self.tool_error else "hi"}],
                "isError": self.tool_error,
            }
            if not self.tool_error:
                result["structuredContent"] = {"echoed": body["params"]["arguments"]["text"]}
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        response = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            return Response(f"event: message\ndata: {json.dumps(response)}\n\n", media_type="text/event-stream")
        return JSONResponse(response)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return [post for post in self.posts if post["method"] == "tools/call"]


@pytest.fixture
def sse() -> bool:
    return False


# Directly parametrizing `sse` overrides the fixture for the whole fixture chain of the test.
REPLY_ENCODINGS = pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])


@pytest.fixture
def fake_server(sse: bool) -> FakeMcpServer:
    return FakeMcpServer(sse=sse)


def mcp_binding(group: ActionGroup) -> McpExecutorBinding:
    """This file builds only MCP groups, so narrowing the union is an assertion, not a branch."""
    assert isinstance(group.executor, McpExecutorBinding)
    return group.executor


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
    async with executor:
        await wait_available(http_group)
        yield executor


@pytest.fixture
def execution_request() -> ExecutionRequest:
    return ExecutionRequest(
        request_id=uuid4(),
        action=ActionIdentity(group="remote", name="echo"),
        arguments={"text": "hi"},
        caller=ServiceAccountRef(namespace="agentplane-test", name="test-http-caller"),
    )


@REPLY_ENCODINGS
async def test_http_session_discovery_call_and_shutdown(
    execution_lease: ExecutionLease,
    http_group: ActionGroup,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    executor = McpActionGroupExecutor.from_group("remote", http_group)
    async with executor:
        await wait_available(http_group)
        assert http_group.available
        assert set(http_group.actions) == {"echo"}
        assert http_group.actions["echo"].description == "Echo text over HTTP."
        assert http_group.actions["echo"].input_schema == fake_server.tools[0]["inputSchema"]
        result = await executor.execute(execution_request, execution_lease)
        assert result.state is ExecutionState.SUCCEEDED
        assert result.result == {
            "content": [{"type": "text", "text": "hi"}],
            "structuredContent": {"echoed": "hi"},
            "isError": False,
        }
        assert [post["method"] for post in fake_server.posts] == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/list",
            "tools/call",
        ]
        assert len(fake_server.calls) == 1
        assert fake_server.calls[0]["params"]["name"] == "echo"
        assert fake_server.calls[0]["params"]["arguments"] == {"text": "hi"}
    assert fake_server.requests[-1].method == "DELETE"
    assert all("authorization" not in request.headers for request in fake_server.requests)
    # Neither `server/discover` nor `initialize` itself carries a session id -- the server only
    # assigns one in the `initialize` response, echoed starting with the next request.
    for request in fake_server.requests[2:]:
        assert request.headers["mcp-session-id"] == "test-http-session"
        assert request.headers["mcp-protocol-version"]


async def test_http_configured_headers_are_sent_on_every_request(fake_server: FakeMcpServer) -> None:
    """A configured extra header (e.g. GitHub MCP's `X-MCP-Toolsets`) reaches the backend."""
    app = Starlette(routes=[Route("/test-mcp", fake_server.handle, methods=["POST", "GET", "DELETE"])])
    with serve_app_sync(app) as url:
        group = ActionGroup(
            title="HTTP test group with extra headers",
            description="Credentialless test peer with a configured toolset header",
            executor=McpExecutorBinding(
                kind="mcp",
                description="HTTP test peer",
                config={
                    "transport": "streamable-http",
                    "url": f"{url}/test-mcp",
                    "auth": "none",
                    "headers": {"X-MCP-Toolsets": "default,actions"},
                },
            ),
        )
        executor = McpActionGroupExecutor.from_group("remote", group)
        async with executor:
            await wait_available(group)
    assert fake_server.requests
    assert all(request.headers["x-mcp-toolsets"] == "default,actions" for request in fake_server.requests)


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_server.list_unavailable = True
    fake_server.list_error_body = b"upstream connect error: reset before headers; bearer test-body-secret"
    fake_server.list_error_headers = {"server": "envoy", "set-cookie": "test-cookie-secret"}
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "mcp_unavailable"
    diagnostics = result.error["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert isinstance(diagnostics["exception_type"], str)
    for key in ("connection_age_s", "request_duration_s"):
        duration = diagnostics[key]
        assert isinstance(duration, (int, float))
        assert duration >= 0
    assert "MCP execution schema check failed" in caplog.text
    for secret in ("test-body-secret", "test-cookie-secret"):
        assert secret not in json.dumps(result.error)
        assert secret not in caplog.text
    assert fake_server.calls == []


@REPLY_ENCODINGS
async def test_http_tool_error_output_is_a_successful_result(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.tool_error = True
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.error is None
    assert result.result == {"content": [{"type": "text", "text": "backend tool error text"}], "isError": True}
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


@pytest.mark.parametrize(
    ("unusable", "detail"),
    [
        (SimpleNamespace(status=McpLinkageStatus.UNLINKED), "no account is linked"),
        (
            SimpleNamespace(status=McpLinkageStatus.EXPIRED, expires_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
            "the access token expired at 2026-01-02 03:04:05 UTC and has not been refreshed",
        ),
    ],
)
async def test_oauth_group_without_a_usable_linkage_stays_dormant_and_says_why(
    unusable: SimpleNamespace, detail: str
) -> None:
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
    linkage.status.side_effect = [unusable, SimpleNamespace(status=McpLinkageStatus.LINKED)]
    with patch("agentplane.action_service.mcp_executor.StreamableHttpTransport") as transport:
        executor = McpActionGroupExecutor.from_group_with_linkage("remote", group, linkage)
        connected = asyncio.Event()
        release = asyncio.Event()

        async def connect() -> None:
            connected.set()
            await release.wait()

        with patch.object(executor, "_connect_client", connect):
            async with executor:
                await asyncio.sleep(0)
                assert not group.available
                assert group.actions == {}
                assert transport.call_count == 0
                linkage.status.assert_awaited()
                assert group.health is not None
                assert (group.health.reason, group.health.detail) == ("linkage_unavailable", detail)
                linkage_changed.set()
                async with asyncio.timeout(1):
                    await connected.wait()


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
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "extra_unknown_field": {}},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "headers": {"Authorization": "Bearer x"}},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "headers": {"authorization": "Bearer x"}},
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
        fake_server.tools[0]["inputSchema"] = {
            "type": "object",
            "properties": {"invalid": {"type": "test-invalid-type"}},
        }
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
    fake_server.tools[0]["inputSchema"] = {"type": "object", "properties": {"invalid": {"type": "test-invalid-type"}}}
    result = await executor.execute(execution_request, execution_lease)
    assert result.error == {"kind": "mcp_invalid_schema", "message": "backend tool schema is invalid"}
    assert fake_server.calls == []


@pytest.mark.parametrize("failure", ["unavailable", "invalid_schema"])
async def test_runtime_serves_unavailable_group_and_recovers_without_restart(
    http_group: ActionGroup, fake_server: FakeMcpServer, failure: str
) -> None:
    if failure == "unavailable":
        fake_server.list_unavailable = True
    else:
        fake_server.tools[0]["inputSchema"] = {
            "type": "object",
            "properties": {"invalid": {"type": "test-invalid-type"}},
        }
    catalog = ActionCatalog(groups={"remote": http_group})
    url = str(mcp_binding(http_group).config["url"])
    async with running_executor(catalog):
        assert not http_group.available
        await wait_retry(http_group)
        assert http_group.health is not None
        assert http_group.actions == {}
        # The operator's view says what failed; a workload's never names the backend.
        assert (
            http_group.health.detail
            == {
                "unavailable": "MCPError: Server returned an error response",
                "invalid_schema": "_InvalidMcpCatalogError: tool 'echo' input schema: "
                "'test-invalid-type' is not valid under any of the given schemas",
            }[failure]
        )
        assert url not in "".join(view.model_dump_json() for view in catalog.group_views(with_detail=False))
        assert fake_server.calls == []
        fake_server.list_unavailable = False
        fake_server.tools[0]["inputSchema"] = {"type": "object"}
        await wait_available(http_group)
        assert set(http_group.actions) == {"echo"}
        assert http_group.health.detail is None


async def test_unreachable_backend_reports_the_connection_error() -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))  # Bound but never listening: every connect is refused.
        group = ActionGroup(
            title="Unreachable test group",
            description="Nothing accepts connections here",
            executor=McpExecutorBinding(
                kind="mcp",
                description="Unreachable test peer",
                config={
                    "transport": "streamable-http",
                    "url": f"http://127.0.0.1:{reserved.getsockname()[1]}/test-mcp",
                    "auth": "none",
                },
            ),
        )
        async with running_executor(ActionCatalog(groups={"remote": group})):
            await wait_retry(group)
            assert group.health is not None
            assert (group.health.reason, group.health.detail) == (
                "connect_failed",
                "RuntimeError: Client failed to connect: All connection attempts failed",
            )


async def test_static_credential_mount_recovers(http_group: ActionGroup, tmp_path: Path) -> None:
    token_file = tmp_path / "bearer"
    mcp_binding(http_group).config.update(auth="static_bearer", bearer_file=str(token_file))
    async with running_executor(ActionCatalog(groups={"remote": http_group})):
        await wait_retry(http_group)
        assert http_group.health is not None
        assert not http_group.available
        token_file.write_text("test-only-mounted-token")
        await wait_available(http_group)
        assert set(http_group.actions) == {"echo"}


async def test_established_http_failure_reconnects_fresh_session(
    executor: McpActionGroupExecutor,
    http_group: ActionGroup,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
    execution_lease: ExecutionLease,
) -> None:
    old = executor._connection
    fake_server.list_unavailable = True
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert fake_server.calls == []
    assert not http_group.available
    fake_server.list_unavailable = False
    await wait_available(http_group)
    assert executor._connection is not old
    assert sum(post["method"] == "initialize" for post in fake_server.posts) >= 2
    assert (await executor.execute(execution_request, execution_lease)).state is ExecutionState.SUCCEEDED
    assert len(fake_server.calls) == 1


if __name__ == "__main__":
    pytest_bazel.main()
