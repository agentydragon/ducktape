from email.message import Message
from http import HTTPStatus
from urllib import error

import provision
import pytest_bazel


def http_error(path: str, code: HTTPStatus) -> error.HTTPError:
    return error.HTTPError(f"http://home-assistant{path}", code, code.phrase, Message(), None)


def disable_http_configuration(monkeypatch):
    async def fake_configure_http(password: str, token: str) -> None:
        pass

    monkeypatch.setattr(provision, "configure_http", fake_configure_http)


async def test_fresh_install_creates_owner_and_completes_onboarding(monkeypatch):
    disable_http_configuration(monkeypatch)
    calls: list[tuple[str, dict[str, object] | None, str | None, bool]] = []

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
    ) -> object:
        calls.append((path, data, token, form))
        if path == "/api/":
            raise http_error(path, HTTPStatus.UNAUTHORIZED)
        if path == "/api/onboarding":
            return [{"step": "user", "done": False}]
        if path == "/api/onboarding/users":
            return {"auth_code": "owner-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        return {}

    monkeypatch.setattr(provision, "request_json", fake_request)
    await provision.provision("secret-password")

    assert calls == [
        ("/api/", None, None, False),
        ("/api/onboarding", None, None, False),
        (
            "/api/onboarding/users",
            {
                "name": provision.DISPLAY_NAME,
                "username": provision.USERNAME,
                "password": "secret-password",
                "client_id": provision.CLIENT_ID,
                "language": "en",
            },
            None,
            False,
        ),
        (
            "/auth/token",
            {"grant_type": "authorization_code", "code": "owner-code", "client_id": provision.CLIENT_ID},
            None,
            True,
        ),
        ("/api/onboarding/core_config", {}, "bootstrap-token", False),
        (
            "/api/onboarding/integration",
            {"client_id": provision.CLIENT_ID, "redirect_uri": provision.REDIRECT_URI},
            "bootstrap-token",
            False,
        ),
        ("/api/onboarding/analytics", {}, "bootstrap-token", False),
    ]


async def test_partial_run_logs_in_and_finishes_remaining_steps(monkeypatch):
    disable_http_configuration(monkeypatch)
    calls: list[str] = []

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
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

    monkeypatch.setattr(provision, "request_json", fake_request)
    await provision.provision("secret-password")

    assert calls == [
        "/api/",
        "/api/onboarding",
        "/auth/login_flow",
        "/auth/login_flow/login-flow",
        "/auth/token",
        "/api/onboarding/integration",
        "/api/onboarding/analytics",
    ]


async def test_completed_onboarding_converges_http_configuration(monkeypatch):
    disable_http_configuration(monkeypatch)
    calls: list[str] = []

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
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

    monkeypatch.setattr(provision, "request_json", fake_request)
    await provision.provision("secret-password")

    assert calls == ["/api/", "/api/onboarding", "/auth/login_flow", "/auth/login_flow/login-flow", "/auth/token"]


async def test_onboarding_404_is_only_accepted_after_the_api_is_ready(monkeypatch):
    disable_http_configuration(monkeypatch)
    calls: list[str] = []

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
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

    monkeypatch.setattr(provision, "request_json", fake_request)
    monkeypatch.setattr(provision.time, "sleep", lambda _: None)
    await provision.provision("secret-password")

    assert calls == [
        "/api/",
        "/api/",
        "/api/onboarding",
        "/auth/login_flow",
        "/auth/login_flow/login-flow",
        "/auth/token",
    ]


async def test_completed_onboarding_steps_allow_future_additions(monkeypatch):
    disable_http_configuration(monkeypatch)
    calls: list[str] = []

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
    ) -> object:
        calls.append(path)
        if path == "/api/":
            raise http_error(path, HTTPStatus.UNAUTHORIZED)
        if path == "/api/onboarding":
            return [{"step": step, "done": True} for step in [*provision.REQUIRED_STEPS, "future_step"]]
        if path == "/auth/login_flow":
            return {"flow_id": "login-flow"}
        if path == "/auth/login_flow/login-flow":
            return {"result": "login-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        raise AssertionError(f"unexpected request: {path}")

    monkeypatch.setattr(provision, "request_json", fake_request)
    await provision.provision("secret-password")

    assert calls == ["/api/", "/api/onboarding", "/auth/login_flow", "/auth/login_flow/login-flow", "/auth/token"]


async def test_configure_http_is_idempotent(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_websocket_command(token: str, message: dict[str, object]) -> object:
        calls.append(message)
        return {
            "stable": {**provision.HTTP_CONFIG, "created_at": "now", "error": None, "error_message": None},
            "pending": None,
            "active_config_type": "stable",
        }

    monkeypatch.setattr(provision, "websocket_command", fake_websocket_command)

    await provision.configure_http("secret-password", "bootstrap-token")

    assert calls == [{"id": 1, "type": "http/config"}]


async def test_configure_http_restarts_and_promotes(monkeypatch):
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_websocket_command(token: str, message: dict[str, object]) -> object:
        calls.append((token, message))
        if message["type"] == "http/config":
            return {"stable": {"server_port": 8124}, "pending": None, "active_config_type": "stable"}
        if message["type"] == "http/config/configure":
            return {"restart": True}
        if message["type"] == "http/config/promote":
            return None
        raise AssertionError(f"unexpected message: {message}")

    monkeypatch.setattr(provision, "websocket_command", fake_websocket_command)
    monkeypatch.setattr(provision, "wait_for_home_assistant", lambda: None)
    monkeypatch.setattr(provision, "login", lambda password: "refreshed-login-code")
    monkeypatch.setattr(provision, "exchange_token", lambda auth_code: "refreshed-token")

    await provision.configure_http("secret-password", "bootstrap-token")

    assert calls == [
        ("bootstrap-token", {"id": 1, "type": "http/config"}),
        ("bootstrap-token", {"id": 1, "type": "http/config/configure", "config": provision.HTTP_CONFIG}),
        ("refreshed-token", {"id": 1, "type": "http/config/promote"}),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
