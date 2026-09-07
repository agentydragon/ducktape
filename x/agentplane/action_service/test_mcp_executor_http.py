"""Pinned FastMCP/MCP client against a deterministic streamable-HTTP peer on a real socket.

Both JSON and finite SSE replies exercise the production from_group transport selection. The
peer records whole JSON-RPC payloads, and can fail after accepting a call without relying on timing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from util.testing.asgi import serve_app_sync
from x.agentplane.action_service.catalog import ActionGroup, ActionIdentity, McpExecutorBinding
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionState
from x.agentplane.action_service.service import ExecutionOutcomeUnknownError


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
                config={"transport": "streamable-http", "url": f"{url}/test-mcp"},
            ),
        )


@pytest.fixture
async def executor(http_group: ActionGroup, fake_server: FakeMcpServer) -> AsyncIterator[McpActionGroupExecutor]:
    executor = McpActionGroupExecutor.from_group("remote", http_group, catalog_refresh_interval=timedelta(hours=1))
    try:
        await executor.start()
        yield executor
    finally:
        # Under mcp-sdk v1 the pinned client re-raised a background session task's terminal HTTP
        # failure when joining it here; under v2, closing no longer re-raises it (confirmed
        # empirically -- the fake_server.list_unavailable/call_unavailable cases below now close
        # cleanly), so there is no longer a case to special-case.
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
        assert result.result == {"echoed": "hi"}
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
    finally:
        await executor.close()
    assert fake_server.requests[-1].method == "DELETE"
    assert all("authorization" not in request.headers for request in fake_server.requests)
    # Neither `server/discover` nor `initialize` itself carries a session id -- the server only
    # assigns one in the `initialize` response, echoed starting with the next request.
    for request in fake_server.requests[2:]:
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


@pytest.mark.parametrize(
    "config",
    [
        {"transport": "streamable-http", "url": "file:///tmp/test-mcp"},
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


if __name__ == "__main__":
    pytest_bazel.main()
