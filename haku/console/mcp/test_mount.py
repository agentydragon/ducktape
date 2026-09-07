"""Tests for `mount_mcp_app`'s wedged-session-manager health tracking."""

from __future__ import annotations

import pytest
import pytest_bazel
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from haku.console.mcp.mount import McpSessionManagerHealth, mount_mcp_app

_WEDGE_MESSAGE = "FastMCP's StreamableHTTPSessionManager task group was not initialized.\nOriginal error: ..."


class _RaisingAsgiApp:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise self._exc


async def _healthy_asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ok")(scope, receive, send)


def test_health_starts_alive() -> None:
    assert McpSessionManagerHealth().alive is True


def test_observe_ignores_unrelated_runtime_errors() -> None:
    health = McpSessionManagerHealth()
    health.observe(RuntimeError("some other failure"))
    assert health.alive is True


def test_observe_marks_dead_on_session_manager_wedge() -> None:
    health = McpSessionManagerHealth()
    health.observe(RuntimeError(_WEDGE_MESSAGE))
    assert health.alive is False


def test_mount_mcp_app_forwards_healthy_requests() -> None:
    app = Starlette()
    health = McpSessionManagerHealth()
    mount_mcp_app(app, path="/mcp", mcp_app=_healthy_asgi_app, health=health)

    response = TestClient(app).post("/mcp")

    assert response.status_code == 200
    assert health.alive is True


def test_mount_mcp_app_observes_wedge_and_still_returns_500() -> None:
    app = Starlette()
    health = McpSessionManagerHealth()
    mount_mcp_app(app, path="/mcp", mcp_app=_RaisingAsgiApp(RuntimeError(_WEDGE_MESSAGE)), health=health)

    with pytest.raises(RuntimeError, match="StreamableHTTPSessionManager"):
        TestClient(app, raise_server_exceptions=True).post("/mcp")

    assert health.alive is False


def test_mount_mcp_app_ignores_unrelated_failures() -> None:
    app = Starlette()
    health = McpSessionManagerHealth()
    mount_mcp_app(app, path="/mcp", mcp_app=_RaisingAsgiApp(RuntimeError("db unavailable")), health=health)

    with pytest.raises(RuntimeError, match="db unavailable"):
        TestClient(app, raise_server_exceptions=True).post("/mcp")

    assert health.alive is True


if __name__ == "__main__":
    pytest_bazel.main()
