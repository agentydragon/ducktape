import json
from http import HTTPStatus
from urllib.parse import parse_qs

import httpx2
import pytest
import pytest_bazel
import respx

from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.onboarding.onboard import configure_core, configure_http, onboard
from homeassistant.provisioner.onboarding.settings import CoreConfig, HomeLocation, HttpConfig, Settings

# The httpx2_mock fixture comes from the auto-loaded pytest-httpx2 plugin.
# gazelle:include_dep @pypi//pytest_httpx2

BASE_URL = "http://home-assistant.test:8123"
pytestmark = pytest.mark.httpx2(base_url=BASE_URL, assert_all_called=False)


@pytest.fixture
def settings(endpoint: HomeAssistantEndpoint) -> Settings:
    return Settings(
        endpoint=endpoint,
        owner_username="test-admin",
        owner_display_name="Test Administrator",
        owner_password="secret-password",
        http_config=HttpConfig(
            server_host=["127.0.0.1"],
            server_port=8124,
            cors_allowed_origins=["https://cast.test"],
            use_x_forwarded_for=True,
            trusted_proxies=["127.0.0.1/32"],
            login_attempts_threshold=-1,
            ip_ban_enabled=True,
            ssl_profile="modern",
            use_x_frame_options=True,
        ),
        core_config=CoreConfig(time_zone="Etc/GMT+5"),
    )


def disable_admin_configuration(monkeypatch):
    """The onboarding tests follow its HTTP requests; the settings it converges over the websocket
    have their own tests below."""

    async def skip(*args) -> None:
        pass

    monkeypatch.setattr("homeassistant.provisioner.onboarding.onboard.configure_http", skip)
    monkeypatch.setattr("homeassistant.provisioner.onboarding.onboard.configure_core", skip)


def assert_request_paths(router: respx.Router, paths: list[str]) -> None:
    assert [call.request.url.path for call in router.calls] == paths


def http_status_error(status: HTTPStatus) -> httpx2.HTTPStatusError:
    request = httpx2.Request("GET", f"{BASE_URL}/api/")
    response = httpx2.Response(status, request=request)
    return httpx2.HTTPStatusError(f"HTTP {status}", request=request, response=response)


async def test_fresh_install_creates_owner_and_completes_onboarding(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, settings
):
    disable_admin_configuration(monkeypatch)
    httpx2_mock.get("/api/").respond(status_code=HTTPStatus.UNAUTHORIZED)
    httpx2_mock.get("/api/onboarding").respond(json=[{"step": "user", "done": False}])
    owner = httpx2_mock.post("/api/onboarding/users").respond(json={"auth_code": "owner-code"})
    token = httpx2_mock.post("/auth/token").respond(json={"access_token": "bootstrap-token"})
    core_config = httpx2_mock.post("/api/onboarding/core_config").respond(json={})
    integration = httpx2_mock.post("/api/onboarding/integration").respond(json={})
    analytics = httpx2_mock.post("/api/onboarding/analytics").respond(json={})

    await onboard(home_assistant_client, settings)

    assert_request_paths(
        httpx2_mock,
        [
            "/api/",
            "/api/onboarding",
            "/api/onboarding/users",
            "/auth/token",
            "/api/onboarding/core_config",
            "/api/onboarding/integration",
            "/api/onboarding/analytics",
        ],
    )
    assert json.loads(owner.calls.last.request.content) == {
        "name": settings.owner_display_name,
        "username": settings.owner_username,
        "password": "secret-password",
        "client_id": settings.endpoint.client_id,
        "language": "en",
    }
    assert parse_qs(token.calls.last.request.content.decode()) == {
        "grant_type": ["authorization_code"],
        "code": ["owner-code"],
        "client_id": [settings.endpoint.client_id],
    }
    assert owner.calls.last.request.headers.get("Authorization") is None
    assert token.calls.last.request.headers.get("Authorization") is None
    for route in (core_config, integration, analytics):
        assert route.calls.last.request.headers["Authorization"] == "Bearer bootstrap-token"


async def test_partial_run_logs_in_and_finishes_remaining_steps(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, settings
):
    disable_admin_configuration(monkeypatch)
    httpx2_mock.get("/api/").respond(status_code=HTTPStatus.UNAUTHORIZED)
    httpx2_mock.get("/api/onboarding").respond(
        json=[
            {"step": "user", "done": True},
            {"step": "core_config", "done": True},
            {"step": "integration", "done": False},
            {"step": "analytics", "done": False},
        ]
    )
    login_flow = httpx2_mock.post("/auth/login_flow").respond(json={"flow_id": "login-flow"})
    login_result = httpx2_mock.post("/auth/login_flow/login-flow").respond(json={"result": "login-code"})
    token = httpx2_mock.post("/auth/token").respond(json={"access_token": "bootstrap-token"})
    integration = httpx2_mock.post("/api/onboarding/integration").respond(json={})
    analytics = httpx2_mock.post("/api/onboarding/analytics").respond(json={})

    await onboard(home_assistant_client, settings)

    assert_request_paths(
        httpx2_mock,
        [
            "/api/",
            "/api/onboarding",
            "/auth/login_flow",
            "/auth/login_flow/login-flow",
            "/auth/token",
            "/api/onboarding/integration",
            "/api/onboarding/analytics",
        ],
    )
    assert json.loads(login_flow.calls.last.request.content) == {
        "client_id": "https://home.test/",
        "handler": ["homeassistant", None],
        "redirect_uri": "https://home.test/",
    }
    assert json.loads(login_result.calls.last.request.content) == {
        "client_id": "https://home.test/",
        "username": "test-admin",
        "password": "secret-password",
    }
    assert parse_qs(token.calls.last.request.content.decode())["code"] == ["login-code"]
    assert integration.calls.last.request.headers["Authorization"] == "Bearer bootstrap-token"
    assert analytics.calls.last.request.headers["Authorization"] == "Bearer bootstrap-token"


async def test_completed_onboarding_converges_http_configuration(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, settings
):
    disable_admin_configuration(monkeypatch)
    httpx2_mock.get("/api/").respond(status_code=HTTPStatus.UNAUTHORIZED)
    httpx2_mock.get("/api/onboarding").respond(status_code=HTTPStatus.NOT_FOUND)
    httpx2_mock.post("/auth/login_flow").respond(json={"flow_id": "login-flow"})
    httpx2_mock.post("/auth/login_flow/login-flow").respond(json={"result": "login-code"})
    httpx2_mock.post("/auth/token").respond(json={"access_token": "bootstrap-token"})

    await onboard(home_assistant_client, settings)

    assert_request_paths(
        httpx2_mock, ["/api/", "/api/onboarding", "/auth/login_flow", "/auth/login_flow/login-flow", "/auth/token"]
    )


async def test_onboarding_404_is_only_accepted_after_the_api_is_ready(
    monkeypatch, httpx2_mock: respx.Router, home_assistant_client, settings
):
    disable_admin_configuration(monkeypatch)
    httpx2_mock.get("/api/").mock(
        side_effect=[http_status_error(HTTPStatus.NOT_FOUND), http_status_error(HTTPStatus.UNAUTHORIZED)]
    )
    httpx2_mock.get("/api/onboarding").respond(status_code=HTTPStatus.NOT_FOUND)
    httpx2_mock.post("/auth/login_flow").respond(json={"flow_id": "login-flow"})
    httpx2_mock.post("/auth/login_flow/login-flow").respond(json={"result": "login-code"})
    httpx2_mock.post("/auth/token").respond(json={"access_token": "bootstrap-token"})
    home_assistant_client.readiness_retry_interval_secs = 0
    await onboard(home_assistant_client, settings)

    assert_request_paths(
        httpx2_mock,
        ["/api/", "/api/", "/api/onboarding", "/auth/login_flow", "/auth/login_flow/login-flow", "/auth/token"],
    )


async def test_configure_http_is_idempotent(monkeypatch, home_assistant_client, settings):
    calls: list[dict[str, object]] = []

    home_assistant_client._access_token = "bootstrap-token"

    async def fake_websocket_command(message: dict[str, object]) -> object:
        calls.append(message)
        return {
            "stable": {**settings.http_config.model_dump(), "created_at": "now", "error": None, "error_message": None},
            "pending": None,
            "active_config_type": "stable",
        }

    monkeypatch.setattr(home_assistant_client, "websocket_command", fake_websocket_command)

    await configure_http(home_assistant_client, settings.http_config, settings.owner_username, "secret-password")

    assert calls == [{"id": 1, "type": "http/config"}]


async def test_configure_http_restarts_and_promotes(monkeypatch, home_assistant_client, settings):
    calls: list[tuple[str, dict[str, object]]] = []

    home_assistant_client._access_token = "bootstrap-token"

    async def fake_websocket_command(message: dict[str, object]) -> object:
        assert home_assistant_client._access_token is not None
        calls.append((home_assistant_client._access_token, message))
        if message["type"] == "http/config":
            return {"stable": {"server_port": 8124}, "pending": None, "active_config_type": "stable"}
        if message["type"] == "http/config/configure":
            return {"restart": True}
        if message["type"] == "http/config/promote":
            return None
        raise AssertionError(f"unexpected message: {message}")

    monkeypatch.setattr(home_assistant_client, "websocket_command", fake_websocket_command)

    async def wait_until_ready():
        return frozenset()

    async def login(username, password):
        home_assistant_client._access_token = "refreshed-token"

    monkeypatch.setattr(home_assistant_client, "wait_until_ready", wait_until_ready)
    monkeypatch.setattr(home_assistant_client, "login", login)

    await configure_http(home_assistant_client, settings.http_config, settings.owner_username, "secret-password")

    assert calls == [
        ("bootstrap-token", {"id": 1, "type": "http/config"}),
        ("bootstrap-token", {"id": 1, "type": "http/config/configure", "config": settings.http_config.model_dump()}),
        ("refreshed-token", {"id": 1, "type": "http/config/promote"}),
    ]


def fake_core_api(monkeypatch, home_assistant_client, current: dict[str, object]) -> list[dict[str, object]]:
    """Answers `get_config` with `current`, records every command, and applies each update to it."""
    calls: list[dict[str, object]] = []

    async def fake_websocket_command(message: dict[str, object]) -> object:
        calls.append(message)
        if message["type"] == "get_config":
            return dict(current)
        if message["type"] == "config/core/update":
            current.update({key: value for key, value in message.items() if key not in {"id", "type"}})
            return None
        raise AssertionError(f"unexpected message: {message}")

    monkeypatch.setattr(home_assistant_client, "websocket_command", fake_websocket_command)
    return calls


# What a freshly onboarded Home Assistant reports: its defaults, which onboarding by API accepts.
FRESH_CORE_CONFIG = {"time_zone": "UTC", "latitude": 0.0, "longitude": 0.0, "location_name": "Home"}


async def test_configure_core_sets_the_time_zone_and_leaves_an_undeclared_location(monkeypatch, home_assistant_client):
    calls = fake_core_api(monkeypatch, home_assistant_client, dict(FRESH_CORE_CONFIG))

    await configure_core(home_assistant_client, CoreConfig(time_zone="Etc/GMT+5"))

    assert calls == [{"id": 1, "type": "get_config"}, {"id": 1, "type": "config/core/update", "time_zone": "Etc/GMT+5"}]


async def test_configure_core_sets_a_declared_location(monkeypatch, home_assistant_client):
    calls = fake_core_api(monkeypatch, home_assistant_client, dict(FRESH_CORE_CONFIG))
    location = HomeLocation(latitude=12.5, longitude=-45.25)

    await configure_core(home_assistant_client, CoreConfig(time_zone="Etc/GMT+5", location=location))

    assert calls[-1] == {
        "id": 1,
        "type": "config/core/update",
        "time_zone": "Etc/GMT+5",
        "latitude": 12.5,
        "longitude": -45.25,
    }


async def test_configure_core_is_idempotent(monkeypatch, home_assistant_client):
    current = {**FRESH_CORE_CONFIG, "time_zone": "Etc/GMT+5", "latitude": 12.5, "longitude": -45.25}
    calls = fake_core_api(monkeypatch, home_assistant_client, current)
    location = HomeLocation(latitude=12.5, longitude=-45.25)

    await configure_core(home_assistant_client, CoreConfig(time_zone="Etc/GMT+5", location=location))

    assert calls == [{"id": 1, "type": "get_config"}]


if __name__ == "__main__":
    pytest_bazel.main()
