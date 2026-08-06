"""Tests for the HA-MCP Home Assistant token provisioner."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_bazel
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from cluster.provisioners.ha_mcp_token_provisioner import provision as provision_module
from cluster.provisioners.ha_mcp_token_provisioner.provision import (
    CLIENT_ID,
    CLIENT_NAME,
    LIFESPAN_DAYS,
    TOKEN_SECRET_NAME,
    TOKEN_SECRET_NAMESPACE,
    USERNAME,
    login,
    provision,
    read_token_secret,
    replace_long_lived_token,
    token_is_valid,
    write_token_secret,
)


class _FakeCoreV1:
    def __init__(self, secret: object | None = None):
        self.secret = secret
        self.created: list[tuple[str, client.V1Secret]] = []
        self.patched: list[tuple[str, str, client.V1Secret]] = []

    def read_namespaced_secret(self, name: str, namespace: str) -> object:
        assert (name, namespace) == (TOKEN_SECRET_NAME, TOKEN_SECRET_NAMESPACE)
        if self.secret is None:
            raise ApiException(status=404)
        return self.secret

    def create_namespaced_secret(self, namespace: str, secret: client.V1Secret) -> None:
        self.created.append((namespace, secret))

    def patch_namespaced_secret(self, name: str, namespace: str, secret: client.V1Secret) -> None:
        self.patched.append((name, namespace, secret))


def _http(handler) -> httpx.Client:
    return httpx.Client(base_url="http://home-assistant", transport=httpx.MockTransport(handler))


def test_token_is_valid_accepts_success_and_rejects_auth_errors():
    with _http(lambda request: httpx.Response(200)) as http:
        assert token_is_valid(http, "good-token") is True
    with _http(lambda request: httpx.Response(401)) as http:
        assert token_is_valid(http, "bad-token") is False


def test_token_is_valid_raises_other_errors():
    with _http(lambda request: httpx.Response(503)) as http, pytest.raises(httpx.HTTPStatusError):
        token_is_valid(http, "token")


def test_login_runs_home_assistant_login_flow():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/login_flow":
            return httpx.Response(200, json={"flow_id": "flow-1"})
        if request.url.path == "/auth/login_flow/flow-1":
            return httpx.Response(200, json={"result": "auth-code"})
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "access-token"})
        raise AssertionError(request.url)

    with _http(handler) as http:
        assert login(http, "password") == "access-token"

    assert json.loads(requests[0].content) == {
        "client_id": CLIENT_ID,
        "handler": ["homeassistant", None],
        "redirect_uri": CLIENT_ID,
    }
    assert json.loads(requests[1].content) == {"client_id": CLIENT_ID, "username": USERNAME, "password": "password"}
    assert requests[2].content.decode() == (
        "grant_type=authorization_code&code=auth-code&client_id=https%3A%2F%2Fhome.allegedly.works%2F"
    )


class _FakeWebsocket:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.sent: list[dict[str, object]] = []

    async def recv(self) -> str:
        return next(self.responses)

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))


def _connect_to(websocket: _FakeWebsocket, urls: list[str]):
    class FakeConnection:
        async def __aenter__(self) -> _FakeWebsocket:
            return websocket

        async def __aexit__(self, *args: object) -> None:
            pass

    def connect(url: str) -> FakeConnection:
        urls.append(url)
        return FakeConnection()

    return connect


def _tokens_reply(message_id: int, tokens: list[dict[str, object]]) -> str:
    return json.dumps({"id": message_id, "success": True, "result": tokens})


async def test_replace_long_lived_token_revokes_the_stale_token_before_minting(monkeypatch: pytest.MonkeyPatch):
    """The deadlock this guards: Home Assistant refuses a duplicate client_name."""
    websocket = _FakeWebsocket(
        [
            '{"type":"auth_required"}',
            '{"type":"auth_ok"}',
            _tokens_reply(
                1,
                [
                    {"id": "session", "type": "normal", "client_name": None},
                    {"id": "other-app", "type": "long_lived_access_token", "client_name": "something-else"},
                    {"id": "stale", "type": "long_lived_access_token", "client_name": CLIENT_NAME},
                ],
            ),
            '{"id":2,"success":true,"result":{}}',
            '{"id":3,"success":true,"result":"fresh-token"}',
        ]
    )
    urls: list[str] = []
    monkeypatch.setattr(provision_module.websockets, "connect", _connect_to(websocket, urls))

    assert await replace_long_lived_token("access-token", "ws://home-assistant/api/websocket") == "fresh-token"
    assert urls == ["ws://home-assistant/api/websocket"]
    assert websocket.sent == [
        {"type": "auth", "access_token": "access-token"},
        {"id": 1, "type": "auth/refresh_tokens"},
        {"id": 2, "type": "auth/delete_refresh_token", "refresh_token_id": "stale"},
        {"id": 3, "type": "auth/long_lived_access_token", "client_name": CLIENT_NAME, "lifespan": LIFESPAN_DAYS},
    ]


async def test_replace_long_lived_token_mints_directly_when_none_exists(monkeypatch: pytest.MonkeyPatch):
    websocket = _FakeWebsocket(
        [
            '{"type":"auth_required"}',
            '{"type":"auth_ok"}',
            _tokens_reply(1, [{"id": "session", "type": "normal", "client_name": None}]),
            '{"id":2,"success":true,"result":"fresh-token"}',
        ]
    )
    monkeypatch.setattr(provision_module.websockets, "connect", _connect_to(websocket, []))

    assert await replace_long_lived_token("access-token", "ws://ha/api/websocket") == "fresh-token"
    assert [message["type"] for message in websocket.sent] == [
        "auth",
        "auth/refresh_tokens",
        "auth/long_lived_access_token",
    ]


async def test_replace_long_lived_token_skips_unsolicited_events(monkeypatch: pytest.MonkeyPatch):
    """A reply is matched by id, so an interleaved event is not mistaken for one."""
    websocket = _FakeWebsocket(
        [
            '{"type":"auth_required"}',
            '{"type":"auth_ok"}',
            '{"id":99,"type":"event","event":{}}',
            _tokens_reply(1, []),
            '{"id":2,"success":true,"result":"fresh-token"}',
        ]
    )
    monkeypatch.setattr(provision_module.websockets, "connect", _connect_to(websocket, []))

    assert await replace_long_lived_token("access-token", "ws://ha/api/websocket") == "fresh-token"


async def test_replace_long_lived_token_surfaces_rejection(monkeypatch: pytest.MonkeyPatch):
    websocket = _FakeWebsocket(
        [
            '{"type":"auth_required"}',
            '{"type":"auth_ok"}',
            _tokens_reply(1, []),
            '{"id":2,"success":false,"error":{"code":"unknown_error","message":"Unknown error"}}',
        ]
    )
    monkeypatch.setattr(provision_module.websockets, "connect", _connect_to(websocket, []))

    with pytest.raises(RuntimeError, match="auth/long_lived_access_token"):
        await replace_long_lived_token("access-token", "ws://ha/api/websocket")


def test_read_token_secret_handles_missing_and_decodes_existing():
    assert read_token_secret(_FakeCoreV1()) == (False, None)
    encoded = base64.b64encode(b"secret-token").decode()
    assert read_token_secret(_FakeCoreV1(SimpleNamespace(data={"token": encoded}))) == (True, "secret-token")


def test_write_token_secret_creates_or_patches():
    v1 = _FakeCoreV1()
    write_token_secret(v1, exists=False, token="secret-token")
    assert v1.created[0][0] == TOKEN_SECRET_NAMESPACE
    assert v1.created[0][1].string_data == {"token": "secret-token"}

    write_token_secret(v1, exists=True, token="replacement")
    assert v1.patched[0][:2] == (TOKEN_SECRET_NAME, TOKEN_SECRET_NAMESPACE)
    assert v1.patched[0][2].string_data == {"token": "replacement"}


def test_provision_keeps_valid_token_without_logging_or_minting(capsys: pytest.CaptureFixture[str]):
    encoded = base64.b64encode(b"valid-token").decode()
    v1 = _FakeCoreV1(SimpleNamespace(data={"token": encoded}))
    with _http(lambda request: httpx.Response(200)) as http:
        assert provision(v1, http, "password", lambda access_token: pytest.fail("must not mint")) is False
    assert "valid-token" not in capsys.readouterr().out
    assert not v1.created
    assert not v1.patched


def test_provision_replaces_rejected_token():
    encoded = base64.b64encode(b"rejected-token").decode()
    v1 = _FakeCoreV1(SimpleNamespace(data={"token": encoded}))

    def handler(request: httpx.Request) -> httpx.Response:
        responses = {
            "/api/": httpx.Response(401),
            "/auth/login_flow": httpx.Response(200, json={"flow_id": "flow"}),
            "/auth/login_flow/flow": httpx.Response(200, json={"result": "code"}),
            "/auth/token": httpx.Response(200, json={"access_token": "access"}),
        }
        return responses[request.url.path]

    with _http(handler) as http:
        assert provision(v1, http, "password", lambda access_token: f"minted-for-{access_token}") is True
    assert v1.patched[0][2].string_data == {"token": "minted-for-access"}


if __name__ == "__main__":
    pytest_bazel.main()
