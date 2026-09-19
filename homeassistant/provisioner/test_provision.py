from http import HTTPStatus
from typing import cast

import aiohttp
import provision
import pytest
import pytest_bazel
from client import HomeAssistantApiError, HomeAssistantClient
from pydantic import ValidationError
from settings import ComponentConfig, HttpConfig, ProvisionerSettings


@pytest.fixture
def provisioner_settings() -> ProvisionerSettings:
    return ProvisionerSettings(
        home_assistant_url="http://home-assistant.test:8123",
        client_id="https://home.test/",
        redirect_uri="https://home.test/",
        username="test-admin",
        display_name="Test Administrator",
        local_admin_password="secret-password",
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
        components=(
            ComponentConfig(
                version="2.2.1",
                url="https://example.test/component.zip",
                sha256="0" * 64,
                archive_path="*/custom_components/example",
                install_dir="example",
                manifest_domain="example",
            ),
            ComponentConfig(
                version="1.1.1",
                url="https://example.test/root-component.zip",
                sha256="0" * 64,
                archive_path=".",
                install_dir="root_component",
                manifest_domain=None,
                config_files=("automations.yaml", "scripts.yaml", "scenes.yaml"),
            ),
        ),
        onboarding_enabled=True,
    )


@pytest.fixture
def home_assistant_client(provisioner_settings: ProvisionerSettings) -> HomeAssistantClient:
    return HomeAssistantClient(cast(aiohttp.ClientSession, object()), provisioner_settings)


def http_error(path: str, code: HTTPStatus) -> HomeAssistantApiError:
    return HomeAssistantApiError(code, code.phrase)


def disable_http_configuration(monkeypatch):
    async def fake_configure_http(self, password: str) -> None:
        pass

    monkeypatch.setattr(HomeAssistantClient, "configure_http", fake_configure_http)


async def test_fresh_install_creates_owner_and_completes_onboarding(
    monkeypatch, home_assistant_client, provisioner_settings
):
    disable_http_configuration(monkeypatch)
    calls: list[tuple[str, dict[str, object] | None, bool, bool]] = []

    async def fake_request(
        self, path: str, *, data: dict[str, object] | None = None, authenticated: bool = True, form: bool = False
    ) -> object:
        calls.append((path, data, authenticated, form))
        if path == "/api/":
            raise http_error(path, HTTPStatus.UNAUTHORIZED)
        if path == "/api/onboarding":
            return [{"step": "user", "done": False}]
        if path == "/api/onboarding/users":
            return {"auth_code": "owner-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        return {}

    monkeypatch.setattr(HomeAssistantClient, "request_json", fake_request)
    await provision.provision(home_assistant_client, "secret-password")

    assert calls == [
        ("/api/", None, False, False),
        ("/api/onboarding", None, False, False),
        (
            "/api/onboarding/users",
            {
                "name": provisioner_settings.display_name,
                "username": provisioner_settings.username,
                "password": "secret-password",
                "client_id": provisioner_settings.client_id,
                "language": "en",
            },
            False,
            False,
        ),
        (
            "/auth/token",
            {"grant_type": "authorization_code", "code": "owner-code", "client_id": provisioner_settings.client_id},
            False,
            True,
        ),
        ("/api/onboarding/core_config", {}, True, False),
        (
            "/api/onboarding/integration",
            {"client_id": provisioner_settings.client_id, "redirect_uri": provisioner_settings.redirect_uri},
            True,
            False,
        ),
        ("/api/onboarding/analytics", {}, True, False),
    ]


async def test_partial_run_logs_in_and_finishes_remaining_steps(
    monkeypatch, home_assistant_client, provisioner_settings
):
    disable_http_configuration(monkeypatch)
    calls: list[str] = []

    async def fake_request(
        self, path: str, *, data: dict[str, object] | None = None, authenticated: bool = True, form: bool = False
    ) -> object:
        calls.append(path)
        if path == "/api/":
            raise http_error(path, HTTPStatus.UNAUTHORIZED)
        if path == "/api/onboarding":
            return [
                {"step": "user", "done": True},
                {"step": "core_config", "done": True},
                {"step": "integration", "done": False},
                {"step": "analytics", "done": False},
            ]
        if path == "/auth/login_flow":
            return {"flow_id": "login-flow"}
        if path == "/auth/login_flow/login-flow":
            return {"result": "login-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        return {}

    monkeypatch.setattr(HomeAssistantClient, "request_json", fake_request)
    await provision.provision(home_assistant_client, "secret-password")

    assert calls == [
        "/api/",
        "/api/onboarding",
        "/auth/login_flow",
        "/auth/login_flow/login-flow",
        "/auth/token",
        "/api/onboarding/integration",
        "/api/onboarding/analytics",
    ]


async def test_completed_onboarding_converges_http_configuration(
    monkeypatch, home_assistant_client, provisioner_settings
):
    disable_http_configuration(monkeypatch)
    calls: list[str] = []

    async def fake_request(
        self, path: str, *, data: dict[str, object] | None = None, authenticated: bool = True, form: bool = False
    ) -> object:
        calls.append(path)
        if path == "/api/":
            raise http_error(path, HTTPStatus.UNAUTHORIZED)
        if path == "/api/onboarding":
            raise http_error(path, HTTPStatus.NOT_FOUND)
        if path == "/auth/login_flow":
            return {"flow_id": "login-flow"}
        if path == "/auth/login_flow/login-flow":
            return {"result": "login-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        raise AssertionError(f"unexpected request: {path}")

    monkeypatch.setattr(HomeAssistantClient, "request_json", fake_request)
    await provision.provision(home_assistant_client, "secret-password")

    assert calls == ["/api/", "/api/onboarding", "/auth/login_flow", "/auth/login_flow/login-flow", "/auth/token"]


async def test_onboarding_404_is_only_accepted_after_the_api_is_ready(
    monkeypatch, home_assistant_client, provisioner_settings
):
    disable_http_configuration(monkeypatch)
    calls: list[str] = []

    async def fake_request(
        self, path: str, *, data: dict[str, object] | None = None, authenticated: bool = True, form: bool = False
    ) -> object:
        calls.append(path)
        if calls == ["/api/"]:
            raise http_error(path, HTTPStatus.NOT_FOUND)
        if path == "/api/":
            raise http_error(path, HTTPStatus.UNAUTHORIZED)
        if path == "/api/onboarding":
            raise http_error(path, HTTPStatus.NOT_FOUND)
        if path == "/auth/login_flow":
            return {"flow_id": "login-flow"}
        if path == "/auth/login_flow/login-flow":
            return {"result": "login-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        raise AssertionError(f"unexpected request: {path}")

    monkeypatch.setattr(HomeAssistantClient, "request_json", fake_request)

    home_assistant_client.readiness_retry_interval_secs = 0
    await provision.provision(home_assistant_client, "secret-password")

    assert calls == [
        "/api/",
        "/api/",
        "/api/onboarding",
        "/auth/login_flow",
        "/auth/login_flow/login-flow",
        "/auth/token",
    ]


@pytest.mark.parametrize(
    "response",
    [[{"step": "future_step", "done": False}], [{"step": "user", "done": 1}], {"step": "user", "done": True}],
)
async def test_onboarding_status_validates_response(monkeypatch, home_assistant_client, response):
    async def fake_request(self, path: str, **kwargs) -> object:
        assert path == "/api/onboarding"
        return response

    monkeypatch.setattr(HomeAssistantClient, "request_json", fake_request)

    with pytest.raises(ValidationError):
        await home_assistant_client.onboarding_status()


async def test_configure_http_is_idempotent(monkeypatch, home_assistant_client, provisioner_settings):
    calls: list[dict[str, object]] = []

    home_assistant_client._access_token = "bootstrap-token"

    async def fake_websocket_command(message: dict[str, object]) -> object:
        calls.append(message)
        return {
            "stable": {
                **provisioner_settings.http_config.model_dump(),
                "created_at": "now",
                "error": None,
                "error_message": None,
            },
            "pending": None,
            "active_config_type": "stable",
        }

    monkeypatch.setattr(home_assistant_client, "websocket_command", fake_websocket_command)

    await home_assistant_client.configure_http("secret-password")

    assert calls == [{"id": 1, "type": "http/config"}]


async def test_configure_http_restarts_and_promotes(monkeypatch, home_assistant_client, provisioner_settings):
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
        return None

    async def login(password):
        home_assistant_client._access_token = "refreshed-token"

    monkeypatch.setattr(home_assistant_client, "wait_until_ready", wait_until_ready)
    monkeypatch.setattr(home_assistant_client, "login", login)

    await home_assistant_client.configure_http("secret-password")

    assert calls == [
        ("bootstrap-token", {"id": 1, "type": "http/config"}),
        (
            "bootstrap-token",
            {"id": 1, "type": "http/config/configure", "config": provisioner_settings.http_config.model_dump()},
        ),
        ("refreshed-token", {"id": 1, "type": "http/config/promote"}),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
