"""Smoke test for the whoami backend.

Asserts that the backend echoes the outpost-injected headers correctly.
Doesn't cover the MCP server side — that needs a full Authentik roundtrip
(or a faked one) and is verified end-to-end in the cluster.
"""

from __future__ import annotations

import pytest_bazel
from fastapi.testclient import TestClient

from x.authentik_mcp_poc.backend import create_app


def test_healthz() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_whoami_reads_outpost_headers() -> None:
    client = TestClient(create_app())
    response = client.get(
        "/whoami",
        headers={
            "X-Authentik-Username": "alice",
            "X-Authentik-Email": "alice@example.com",
            "X-Authentik-Uid": "abc-123",
            "X-Authentik-Groups": "admins|users",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "user": "alice",
        "email": "alice@example.com",
        "uid": "abc-123",
        "groups": ["admins", "users"],
        "secret_message": "auth flowed through the Authentik proxy outpost",
    }


def test_whoami_missing_headers() -> None:
    # With no outpost in the path (direct-to-backend access), the backend
    # returns nulls rather than crashing. In production the direct path is
    # gated by NetworkPolicy; the outpost always injects the headers.
    client = TestClient(create_app())
    response = client.get("/whoami")
    assert response.status_code == 200
    body = response.json()
    assert body["user"] is None
    assert body["email"] is None
    assert body["groups"] == []


if __name__ == "__main__":
    pytest_bazel.main()
