"""Capability-tier tests: the launch-routine action is CSRF-gated, forwards the
server-side bearer to the fire URL, and maps upstream failure / missing config to
clean statuses. The external fire call is mocked with respx (which patches httpx's
real transport, not TestClient's ASGI transport, so app calls still reach the app).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import pytest_bazel
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from haku.console.app import create_app
from haku.console.config import LaunchRoutineConfig

FIRE_URL = "https://fire.test.example/routines/trig_test/fire"


@pytest.fixture
def cap_client(seeded) -> Iterator[TestClient]:
    """Console app with the launch-routine capability configured (over the seeded remote)."""
    settings = seeded.settings.model_copy(
        update={
            "launch_routine": LaunchRoutineConfig(url=FIRE_URL, token=SecretStr("sk-test-token")),
            "csrf_secret": SecretStr("test-csrf-secret"),
        }
    )
    with TestClient(create_app(settings, git_state=seeded.git_state)) as c:
        yield c


def _csrf(client: TestClient) -> str:
    token = client.get("/api/capabilities/csrf").json()["csrf_token"]
    assert isinstance(token, str)
    return token


@respx.mock
def test_launch_routine_fires_with_server_side_bearer(cap_client) -> None:
    route = respx.post(FIRE_URL).mock(return_value=httpx.Response(200, json={"queued": True}))
    resp = cap_client.post("/api/capabilities/launch-routine", headers={"X-CSRF-Token": _csrf(cap_client)})
    assert resp.status_code == 200
    assert resp.json() == {"status": "launched", "upstream_status": 200}
    # The bearer is attached server-side and never returned to the client.
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test-token"


@respx.mock
def test_launch_routine_without_csrf_token_is_rejected(cap_client) -> None:
    route = respx.post(FIRE_URL).mock(return_value=httpx.Response(200))
    resp = cap_client.post("/api/capabilities/launch-routine")
    assert resp.status_code in (400, 401)
    assert not route.called  # rejected before any upstream call


@respx.mock
def test_launch_routine_upstream_failure_is_502(cap_client) -> None:
    respx.post(FIRE_URL).mock(return_value=httpx.Response(500))
    resp = cap_client.post("/api/capabilities/launch-routine", headers={"X-CSRF-Token": _csrf(cap_client)})
    assert resp.status_code == 502


def test_launch_routine_unconfigured_is_503(seeded) -> None:
    settings = seeded.settings.model_copy(update={"csrf_secret": SecretStr("test-csrf-secret")})
    with TestClient(create_app(settings, git_state=seeded.git_state)) as c:
        resp = c.post("/api/capabilities/launch-routine", headers={"X-CSRF-Token": _csrf(c)})
    assert resp.status_code == 503


if __name__ == "__main__":
    pytest_bazel.main()
