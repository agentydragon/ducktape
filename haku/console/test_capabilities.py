"""Capability-tier tests: the launch-routine action is exact-Origin-gated, forwards the
server-side bearer to the fire URL, and maps upstream failure / missing config to
clean statuses. The external fire call is mocked with respx (which patches httpx's
real transport, not TestClient's ASGI transport, so app calls still reach the app).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import pytest_bazel
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from haku.console.config import LaunchRoutineConfig

ROUTINE_ID = "trig_test"
FIRE_URL = f"https://api.anthropic.com/v1/claude_code/routines/{ROUTINE_ID}/fire"
PAGE_URL = f"https://claude.ai/code/routines/{ROUTINE_ID}"


@pytest.fixture
def cap_client(make_operator_client: Callable[..., Any]) -> Iterator[TestClient]:
    """Console app with the launch-routine capability configured (over the seeded remote)."""
    with make_operator_client(
        launch_routine=LaunchRoutineConfig(routine_id=ROUTINE_ID, token=SecretStr("sk-test-token"))
    ) as c:
        yield c


def test_config_surfaces_routine_page_url(cap_client) -> None:
    # The routine deep-link (built from the id) reaches the SPA via the config read.
    assert cap_client.get("/api/config").json()["launch_routine_url"] == PAGE_URL


@respx.mock
def test_launch_routine_fires_with_server_side_bearer(cap_client) -> None:
    session_url = "https://claude.ai/code/session_test123"
    route = respx.post(FIRE_URL).mock(
        return_value=httpx.Response(200, json={"claude_code_session_url": session_url, "type": "routine_fire"})
    )
    resp = cap_client.post("/api/capabilities/launch-routine")
    assert resp.status_code == 200
    assert resp.json() == {"session_url": session_url}
    # The bearer + required anthropic-version header are attached server-side; the
    # bearer is never returned to the client.
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer sk-test-token"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert json.loads(sent.content) == {}


@pytest.mark.parametrize(
    ("text", "fired_body"),
    [
        pytest.param("scan CPAP and summarize anomalies", {"text": "scan CPAP and summarize anomalies"}, id="custom"),
        pytest.param("   ", {}, id="blank_uses_routine_default"),
    ],
)
@respx.mock
def test_launch_routine_forwards_text_unless_blank(cap_client, text: str, fired_body: dict[str, str]) -> None:
    session_url = "https://claude.ai/code/session_text"
    route = respx.post(FIRE_URL).mock(
        return_value=httpx.Response(200, json={"claude_code_session_url": session_url, "type": "routine_fire"})
    )
    resp = cap_client.post("/api/capabilities/launch-routine", json={"text": text})
    assert resp.status_code == 200
    assert json.loads(route.calls.last.request.content) == fired_body


@respx.mock
def test_launch_routine_surfaces_upstream_error_detail(cap_client) -> None:
    respx.post(FIRE_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "anthropic-version: header is required"}})
    )
    resp = cap_client.post("/api/capabilities/launch-routine")
    assert resp.status_code == 502
    # The real upstream reason is propagated to the client, not a bare 502.
    assert "anthropic-version: header is required" in resp.json()["detail"]


@respx.mock
def test_launch_routine_from_another_origin_is_rejected(cap_client) -> None:
    route = respx.post(FIRE_URL).mock(return_value=httpx.Response(200))
    resp = cap_client.post("/api/capabilities/launch-routine", headers={"Origin": "https://haku-ui.test"})
    assert resp.status_code == 403
    assert not route.called  # rejected before any upstream call


@respx.mock
def test_launch_routine_upstream_failure_is_502(cap_client) -> None:
    respx.post(FIRE_URL).mock(return_value=httpx.Response(500))
    resp = cap_client.post("/api/capabilities/launch-routine")
    assert resp.status_code == 502


def test_launch_routine_unconfigured_is_503(make_operator_client: Callable[..., Any]) -> None:
    with make_operator_client() as c:
        resp = c.post("/api/capabilities/launch-routine")
    assert resp.status_code == 503


if __name__ == "__main__":
    pytest_bazel.main()
