from __future__ import annotations

import pytest_bazel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from mcp_infra.oauth_facade.config import FacadeLoggingConfig, FacadeSettings, HttpUpstream, StaticBearerClientAuth
from mcp_infra.oauth_facade.server import build_server
from mcp_infra.static_bearer import StaticBearerGuard

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx


def _guarded_app() -> Starlette:
    async def protected(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("healthy")

    guard = StaticBearerGuard(Starlette(routes=[Route("/mcp", protected)]), token="sekret")
    return Starlette(routes=[Route("/healthz", healthz), Mount("/", app=guard)])


def test_probe_route_bypasses_guard() -> None:
    assert TestClient(_guarded_app()).get("/healthz").status_code == 200


def test_missing_token_rejected() -> None:
    assert TestClient(_guarded_app()).get("/mcp").status_code == 401


def test_wrong_token_rejected() -> None:
    assert TestClient(_guarded_app()).get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_correct_token_allowed() -> None:
    resp = TestClient(_guarded_app()).get("/mcp", headers={"Authorization": "Bearer sekret"})
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_mcp_message_logging_middleware_is_optional() -> None:
    settings = FacadeSettings(
        client_auth=StaticBearerClientAuth(static_bearer="sekret"),
        upstream=HttpUpstream(url="http://upstream.svc:8263/mcp"),
        facade_name="Test Facade",
    )
    server, _client_storage = build_server(settings)
    assert all(middleware.__class__.__name__ != "StructuredLoggingMiddleware" for middleware in server.middleware)


def test_mcp_message_logging_middleware_can_be_enabled() -> None:
    settings = FacadeSettings(
        client_auth=StaticBearerClientAuth(static_bearer="sekret"),
        upstream=HttpUpstream(url="http://upstream.svc:8263/mcp"),
        facade_name="Test Facade",
        logging=FacadeLoggingConfig(
            mcp_messages=True,
            mcp_message_level="DEBUG",
            mcp_payloads=False,
            mcp_payload_length=True,
            mcp_methods=["initialize", "tools/list"],
        ),
    )
    server, _client_storage = build_server(settings)
    logging_middleware = [
        middleware for middleware in server.middleware if middleware.__class__.__name__ == "StructuredLoggingMiddleware"
    ]
    assert len(logging_middleware) == 1
    assert logging_middleware[0].include_payloads is False
    assert logging_middleware[0].include_payload_length is True
    assert logging_middleware[0].methods == ["initialize", "tools/list"]


if __name__ == "__main__":
    pytest_bazel.main()
