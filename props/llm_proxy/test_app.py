"""Smoke test: the standalone LLM proxy app assembles and enforces auth.

Routing/budget logic is covered by the router's own helper tests
(`props/backend/routes/test_llm_*`); this verifies the split-out app wires the
`llm` router + auth dependency correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest_bazel
from fastapi.testclient import TestClient

from props.config import PropsConfig
from props.llm_proxy.app import create_app


def _client() -> TestClient:
    config = PropsConfig(backend_url="http://test", agent_env={})
    # The auth-rejection paths below never touch the DB, so a MagicMock suffices.
    return TestClient(create_app(db=MagicMock(), config=config), raise_server_exceptions=False)


def test_health_ok() -> None:
    with _client() as client:
        assert client.get("/health").text == "ok"
        assert client.get("/readyz").status_code == 200


def test_responses_requires_auth() -> None:
    """An unauthenticated /v1/responses call is 401 — proving the proxy reuses the
    backend auth dependency rather than serving the route unguarded."""
    with _client() as client:
        resp = client.post("/v1/responses", json={"model": "x", "input": "hi"})
        assert resp.status_code == 401


if __name__ == "__main__":
    pytest_bazel.main()
