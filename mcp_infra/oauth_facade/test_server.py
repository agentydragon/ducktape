from __future__ import annotations

import pytest_bazel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from mcp_infra.oauth_facade.server import _StaticBearerGuard


def _guarded_app() -> Starlette:
    async def protected(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("healthy")

    guard = _StaticBearerGuard(Starlette(routes=[Route("/mcp", protected)]), token="sekret")
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


if __name__ == "__main__":
    pytest_bazel.main()
