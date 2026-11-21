"""Smoke test: the standalone LLM proxy app assembles and enforces auth.

Routing/budget logic is covered by the router's own helper tests
(`props/llm_proxy/test_*`); this verifies the split-out app wires the routes +
auth dependency correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from props.config import LLMProxyConfig
from props.llm_proxy.app import create_app

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx


def _client() -> TestClient:
    # The proxy needs only the minimal LLMProxyConfig (just upstreams); the
    # health + auth-rejection paths below never route, so empty upstreams suffice.
    config = LLMProxyConfig()
    # The auth-rejection paths below never touch the DB, so a MagicMock suffices.
    return TestClient(create_app(db=MagicMock(), config=config), raise_server_exceptions=False)


def test_health_ok() -> None:
    with _client() as client:
        assert client.get("/health").text == "ok"
        assert client.get("/readyz").status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/responses", {"model": "x", "input": "hi"}),
        ("/v1/chat/completions", {"model": "x", "messages": []}),
        ("/v1/messages", {"model": "x", "max_tokens": 1, "messages": []}),
    ],
)
def test_llm_routes_require_auth(path: str, body: dict[str, object]) -> None:
    """An unauthenticated call to any LLM route is 401 — proving the proxy reuses the
    backend auth dependency rather than serving the route unguarded."""
    with _client() as client:
        resp = client.post(path, json=body)
        assert resp.status_code == 401


if __name__ == "__main__":
    pytest_bazel.main()
