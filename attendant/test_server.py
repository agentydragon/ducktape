"""Exercise owner session controls and the independently authenticated caller API."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import cast

import pytest
import pytest_bazel
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from attendant.host import Host
from attendant.runtime import Operation, Reply, SessionError
from attendant.server import create_app

_ORIGIN = "https://attendant.example.test"
_OWNER = "example-owner-token"
_ROUTES = {"one": "example-one-token", "two": "example-two-token"}
_ONE_PATH = "/api/providers/one/request"
_ORIGIN_HEADERS = {"Origin": _ORIGIN, "Sec-Fetch-Site": "same-origin"}


class FakeHost:
    def __init__(self) -> None:
        self.providers = dict.fromkeys(_ROUTES)
        self.started = False
        self.closed = False
        self.opened: list[str] = []
        self.verified: list[str] = []
        self.requests: list[tuple[str, Operation]] = []
        self.viewer_headers: list[list[tuple[str, str]]] = []
        self.reply = Reply(
            status=200, body=b'{"data":42}', content_type="application/json; charset=utf-8", csrf="sensitive"
        )
        self.issue: str | None = None

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def open(self, name: str) -> None:
        self.opened.append(name)

    async def status(self, name: str) -> str:
        self.verified.append(name)
        return "ready"

    async def request(self, name: str, operation: Operation) -> Reply:
        self.requests.append((name, operation))
        if self.issue is not None:
            raise SessionError(self.issue)
        return self.reply

    async def websocket(self, websocket: WebSocket) -> None:
        self.viewer_headers.append(websocket.headers.items())
        await websocket.accept()
        await websocket.send_bytes(b"RFB 003.008\n")
        await websocket.receive()


@pytest.fixture
def host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def client(host: FakeHost) -> Iterator[TestClient]:
    app = create_app(cast(Host, host), _OWNER, _ROUTES, public_origin=_ORIGIN)
    with TestClient(app, base_url=_ORIGIN) as client:
        assert host.started
        yield client
    assert host.closed


def _sign_in(client: TestClient) -> None:
    response = client.post("/owner/login", headers=_ORIGIN_HEADERS, json={"token": _OWNER})
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-attendant_owner=")
    assert {"HttpOnly", "SameSite=strict", "Secure", "Path=/"} <= set(cookie.split("; ")[1:])
    assert _OWNER not in cookie


def _route_headers(provider: str = "one") -> dict[str, str]:
    return {"Authorization": f"Bearer {_ROUTES[provider]}"}


def test_bundle_served_under_viewer_policy(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert '<script type="module" src="/main.js"></script>' in index.text
    assert 'href="/main.css"' in index.text
    policy = index.headers["content-security-policy"]
    assert "connect-src 'self' wss://attendant.example.test" in policy
    assert "img-src data:" in policy
    assert "frame-ancestors 'none'" in policy
    for asset in ("/main.js", "/main.css"):
        assert client.get(asset).status_code == 200


def test_owner_controls_require_cookie_and_exact_origin(client: TestClient, host: FakeHost) -> None:
    assert client.get("/owner/providers", headers=_route_headers()).status_code == 401
    assert client.post("/owner/login", json={"token": _OWNER}).status_code == 403
    assert client.post("/owner/login", headers=_ORIGIN_HEADERS, json={"token": _ROUTES["one"]}).status_code == 401
    unknown = "/owner/providers/unknown/verify"
    assert client.post(unknown).status_code == 401
    _sign_in(client)
    assert client.post(unknown).status_code == 403
    assert client.post(unknown, headers=_ORIGIN_HEADERS).status_code == 404
    assert client.get("/owner/providers").json() == {"providers": ["one", "two"]}
    for headers in (
        {},
        {"Origin": "https://attacker.example.test"},
        {**_ORIGIN_HEADERS, "Sec-Fetch-Site": "same-site"},
    ):
        assert client.post("/owner/providers/one/open", headers=headers).status_code == 403
    assert client.post("/owner/providers/unknown/open", headers=_ORIGIN_HEADERS).status_code == 404
    assert host.opened == []
    assert client.post("/owner/providers/one/open", headers=_ORIGIN_HEADERS).status_code == 204
    assert host.opened == ["one"]

    verify = "/owner/providers/one/verify"
    assert client.post(verify, headers=_ORIGIN_HEADERS).json() == {"state": "ready"}
    assert host.verified == ["one"]

    previous_cookie = client.cookies["__Host-attendant_owner"]
    _sign_in(client)
    denied = client.post(verify, headers={**_ORIGIN_HEADERS, "Cookie": f"__Host-attendant_owner={previous_cookie}"})
    assert denied.status_code == 401
    assert host.verified == ["one"]


def test_viewer_admission_and_revocation(client: TestClient, host: FakeHost) -> None:
    url = "wss://attendant.example.test/owner/viewer"
    with pytest.raises(WebSocketDisconnect) as missing_cookie, client.websocket_connect(url, headers=_ORIGIN_HEADERS):
        pytest.fail("viewer admitted without an owner cookie")
    assert missing_cookie.value.code == 4401

    _sign_in(client)
    for denied_url, headers, expected in (
        (url, {}, 4403),
        (url, {"Origin": "https://other.example.test"}, 4403),
        (url, {**_ORIGIN_HEADERS, "Sec-Fetch-Site": "same-site"}, 4403),
        (url + "?token=unused", _ORIGIN_HEADERS, 4403),
        (url, {**_ORIGIN_HEADERS, **_route_headers()}, 4401),
    ):
        with pytest.raises(WebSocketDisconnect) as denied, client.websocket_connect(denied_url, headers=headers):
            pytest.fail("viewer admitted with invalid handshake")
        assert denied.value.code == expected
    assert host.viewer_headers == []

    with client.websocket_connect(url, headers=_ORIGIN_HEADERS) as websocket:
        assert websocket.receive_bytes() == b"RFB 003.008\n"
        assert host.viewer_headers == [[("authorization", f"Bearer {_OWNER}")]]
        assert client.post("/owner/logout", headers=_ORIGIN_HEADERS).status_code == 204
        assert websocket.receive() == {"type": "websocket.close", "code": 4401, "reason": ""}
    assert client.get("/owner/providers").status_code == 401


def test_route_tokens_cannot_cross_scope_or_use_owner_session(client: TestClient, host: FakeHost) -> None:
    _sign_in(client)
    request = {"method": "GET", "target": "/records?q=example"}
    for headers in ({}, _route_headers("two"), {"Authorization": f"Bearer {_OWNER}"}):
        assert client.post(_ONE_PATH, headers=headers, json=request).status_code == 401
    duplicates = [("Authorization", f"Bearer {_ROUTES['one']}")] * 2
    assert client.post(_ONE_PATH, headers=duplicates, json=request).status_code == 401
    assert client.post("/api/providers/two/request", headers=_route_headers(), json=request).status_code == 401
    unknown = client.post("/api/providers/unknown/request?extra=1", headers=_route_headers())
    assert unknown.status_code == 401
    assert unknown.headers["www-authenticate"] == "Bearer"
    assert not host.requests

    result = client.post(_ONE_PATH, headers=_route_headers(), json=request)
    assert result.json() == {"data": 42}
    assert host.requests == [("one", Operation(method="GET", target="/records?q=example"))]
    assert result.headers["content-type"] == "application/json"
    assert result.headers["cache-control"] == "no-store"
    assert "sensitive" not in str(result.headers)


@pytest.mark.parametrize(
    "operation",
    [
        {"method": "OPTIONS", "target": "/records"},
        {"method": "GET", "target": "/records", "body": "unexpected"},
        {"method": "POST", "target": "/records", "headers": {"Authorization": "ignored"}},
        {"method": "POST", "target": "/records", "body": {"a": 1}},
        {"method": "POST", "target": "/records", "body": "\ud800"},
        {"method": "GET", "target": "https://other.example/records"},
        {"method": "GET", "target": "//other.example/records"},
        {"method": "GET", "target": "/../records"},
        {"method": "GET", "target": "/records/%2fsecret"},
        {"method": "GET", "target": "/records#fragment"},
        {"method": "GET", "target": "/records?bad=\n"},
        {"method": "GET", "target": "/records\\secret"},
        {"method": "GET", "target": "/" + "a" * 8192},
    ],
)
def test_reject_unsafe_or_ambiguous_operations(
    client: TestClient, host: FakeHost, operation: dict[str, object]
) -> None:
    response = client.post(
        _ONE_PATH, headers={**_route_headers(), "Content-Type": "application/json"}, content=json.dumps(operation)
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_request"}
    assert not host.requests


def test_size_limit_and_server_owned_reply_headers(client: TestClient, host: FakeHost) -> None:
    response = client.post(
        _ONE_PATH, headers={**_route_headers(), "Content-Type": "application/json"}, content=b" " * (1024 * 1024 + 1)
    )
    assert response.status_code == 413
    assert not host.requests

    host.reply = Reply(status=200, body=b"<script>alert(1)</script>", content_type="text/html", csrf="secret")
    response = client.post(
        _ONE_PATH, headers=_route_headers(), json={"method": "POST", "target": "/records", "body": "{}"}
    )
    assert host.requests == [("one", Operation(method="POST", target="/records", body=b"{}"))]
    assert response.content == host.reply.body
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert set(response.headers).isdisjoint({"location", "set-cookie", "x-csrf-token", "access-control-allow-origin"})


def test_provider_checks_run_after_http_validation(client: TestClient, host: FakeHost) -> None:
    host.issue = "denied"
    response = client.post(_ONE_PATH, headers=_route_headers(), json={"method": "GET", "target": "/records"})
    assert response.status_code == 403
    assert response.json() == {"detail": "denied"}
    host.issue = "sign_in_needed"
    response = client.post(_ONE_PATH, headers=_route_headers(), json={"method": "GET", "target": "/records"})
    assert response.status_code == 409
    assert response.json() == {"detail": "sign_in_needed"}


def test_configuration_requires_separate_tokens(host: FakeHost) -> None:
    with pytest.raises(ValueError, match="distinct"):
        create_app(cast(Host, host), _OWNER, {**_ROUTES, "one": _OWNER})
    with pytest.raises(ValueError, match="distinct"):
        create_app(cast(Host, host), _OWNER, {"one": _ROUTES["one"]})


if __name__ == "__main__":
    pytest_bazel.main()
