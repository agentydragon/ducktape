"""Smoke tests for the standalone registry proxy app."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest_bazel
from fastapi.testclient import TestClient

from props.registry_proxy.app import create_app

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx


def _client() -> TestClient:
    return TestClient(create_app(db=MagicMock()), raise_server_exceptions=False)


def test_health_ok() -> None:
    with _client() as client:
        assert client.get("/health").text == "ok"
        assert client.get("/readyz").status_code == 200


def test_v2_requires_auth() -> None:
    with _client() as client:
        resp = client.get("/v2/")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Basic realm="props"'


if __name__ == "__main__":
    pytest_bazel.main()
