"""Integration tests for the HA proxy app."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_bazel
import respx

from homeassistant_proxy.config import AccessRule, HomeAssistantSettings, Policy, Settings, TokenConfig
from homeassistant_proxy.policy import EntityInfo
from homeassistant_proxy.proxy import _make_app

_HA_URL = "http://ha.test:8123"

_SETTINGS = Settings(
    homeassistant=HomeAssistantSettings(url=_HA_URL, token="ha-internal-token"),
    tokens={
        "agent": TokenConfig(
            secret="proxy-token-abc",
            policy=Policy(
                all=AccessRule(read=True, control=False),
                domains={"light": AccessRule(read=True, control=True)},
                entity_ids={"light.dangerous": AccessRule(read=True, control=False)},
            ),
        )
    },
)

_REGISTRY = {
    "light.kitchen": EntityInfo(entity_id="light.kitchen", device_id="dev_1", area_id="kitchen"),
    "light.dangerous": EntityInfo(entity_id="light.dangerous", device_id="dev_2", area_id="lab"),
    "switch.pump": EntityInfo(entity_id="switch.pump", device_id="dev_3", area_id="basement"),
    "sensor.temp": EntityInfo(entity_id="sensor.temp"),
}

_HEADERS = {"Authorization": "Bearer proxy-token-abc"}


def _mock_registry():
    return patch("homeassistant_proxy.proxy.fetch_registry", new_callable=AsyncMock, return_value=_REGISTRY)


@pytest.fixture
async def client():
    app = _make_app(_SETTINGS)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c, app.router.lifespan_context(app):
        yield c


async def test_health(client: httpx.AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_unauthorized_no_token(client: httpx.AsyncClient):
    resp = await client.get("/api/states")
    assert resp.status_code == 401


async def test_unauthorized_wrong_token(client: httpx.AsyncClient):
    resp = await client.get("/api/states", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@respx.mock
async def test_api_status(client: httpx.AsyncClient):
    respx.get(f"{_HA_URL}/api/").mock(return_value=httpx.Response(200, json={"message": "API running."}))
    resp = await client.get("/api/", headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["message"] == "API running."


@respx.mock
async def test_states_filtered(client: httpx.AsyncClient):
    all_states = [
        {"entity_id": "light.kitchen", "state": "on"},
        {"entity_id": "switch.pump", "state": "off"},
        {"entity_id": "sensor.temp", "state": "22"},
    ]
    respx.get(f"{_HA_URL}/api/states").mock(return_value=httpx.Response(200, json=all_states))
    with _mock_registry():
        resp = await client.get("/api/states", headers=_HEADERS)
    assert resp.status_code == 200
    entity_ids = [s["entity_id"] for s in resp.json()]
    # All have read=true via the "all" rule
    assert "light.kitchen" in entity_ids
    assert "switch.pump" in entity_ids
    assert "sensor.temp" in entity_ids


@respx.mock
async def test_single_state_allowed(client: httpx.AsyncClient):
    respx.get(f"{_HA_URL}/api/states/light.kitchen").mock(
        return_value=httpx.Response(200, json={"entity_id": "light.kitchen", "state": "on"})
    )
    with _mock_registry():
        resp = await client.get("/api/states/light.kitchen", headers=_HEADERS)
    assert resp.status_code == 200


@respx.mock
async def test_service_call_allowed(client: httpx.AsyncClient):
    respx.post(f"{_HA_URL}/api/services/light/turn_on").mock(return_value=httpx.Response(200, json=[]))
    with _mock_registry():
        resp = await client.post("/api/services/light/turn_on", headers=_HEADERS, json={"entity_id": "light.kitchen"})
    assert resp.status_code == 200


@respx.mock
async def test_service_call_denied_entity(client: httpx.AsyncClient):
    with _mock_registry():
        resp = await client.post("/api/services/light/turn_on", headers=_HEADERS, json={"entity_id": "light.dangerous"})
    assert resp.status_code == 403
    assert "light.dangerous" in resp.json()["error"]


@respx.mock
async def test_service_call_denied_domain(client: httpx.AsyncClient):
    # switch.pump: domain=switch, all rule says control=false
    with _mock_registry():
        resp = await client.post("/api/services/switch/turn_on", headers=_HEADERS, json={"entity_id": "switch.pump"})
    assert resp.status_code == 403


@respx.mock
async def test_service_call_no_target_denied(client: httpx.AsyncClient):
    with _mock_registry():
        resp = await client.post("/api/services/homeassistant/restart", headers=_HEADERS, json={})
    assert resp.status_code == 403
    assert "no entity" in resp.json()["error"]


@respx.mock
async def test_service_call_device_target(client: httpx.AsyncClient):
    # dev_1 resolves to light.kitchen, which has control via light domain
    respx.post(f"{_HA_URL}/api/services/light/turn_on").mock(return_value=httpx.Response(200, json=[]))
    with _mock_registry():
        resp = await client.post("/api/services/light/turn_on", headers=_HEADERS, json={"device_id": "dev_1"})
    assert resp.status_code == 200


async def test_blocked_endpoint(client: httpx.AsyncClient):
    resp = await client.get("/api/events", headers=_HEADERS)
    assert resp.status_code == 403


async def test_blocked_arbitrary_path(client: httpx.AsyncClient):
    resp = await client.get("/api/something/else", headers=_HEADERS)
    assert resp.status_code == 403


if __name__ == "__main__":
    pytest_bazel.main()
