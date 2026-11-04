"""End-to-end proxy authorization and credential ownership."""

from pathlib import PurePosixPath

import pytest_bazel
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from platform_sessions.auth import Bearer, BearerAuth
from platform_sessions.host import create_app
from platform_sessions.proxy import HeaderPolicy, ProxyInstance, ProxyRequest, ProxyResponse, install_proxy
from platform_sessions.registry import IntegrationInstance
from platform_sessions.routes import Route


class Settings(BaseModel):
    pass


class Transport:
    def __init__(self):
        self.requests = []
        self.response = ProxyResponse(200, ((b"content-type", b"text/plain"),), b"ok")

    async def request(self, request: ProxyRequest) -> ProxyResponse:
        self.requests.append(request)
        return self.response


def make_client():
    config = IntegrationInstance(
        "example", "fake", True, "https://upstream.test/api", PurePosixPath("/data/example"), 900, Settings()
    )
    transport = Transport()
    policy = HeaderPolicy(request=frozenset(("X-Private-Request",)), response=frozenset(("X-Private-Response",)))
    consumer = Bearer(
        SecretStr("consumer-secret"), routes={"example": (Route("GET", "/orders/*"), Route("FROB", "/orders"))}
    )
    auth = BearerAuth([consumer, Bearer(SecretStr("owner-secret"), owner=True)])
    app = create_app([], "https://service.test", require_owner=lambda request: None, auth=auth)
    install_proxy(app, {"example": ProxyInstance(config, transport, policy)}, auth)
    return TestClient(app, base_url="https://service.test"), transport


def test_authorization_and_provider_specific_header_filtering():
    client, transport = make_client()
    assert client.get("/proxy/example/orders/1").status_code == 401
    assert client.get("/proxy/example/orders/1", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.get("/proxy/example/orders/1/extra", headers={"authorization": "Bearer consumer-secret"}).status_code
        == 403
    )
    assert (
        client.post("/proxy/example/orders/1", headers={"authorization": "Bearer consumer-secret"}).status_code == 403
    )
    assert not transport.requests
    transport.response = ProxyResponse(
        200,
        (
            (b"content-type", b"text/plain"),
            (b"set-cookie", b"session=secret"),
            (b"authentication-info", b"secret"),
            (b"x-private-response", b"secret"),
            (b"x-kept", b"yes"),
            (b"cache-control", b"max-age=5"),
        ),
        b"hello",
    )
    response = client.get(
        "/proxy/example/orders/one?x=1&x=%2f",
        headers={
            "authorization": "Bearer consumer-secret",
            "cookie": "caller=secret",
            "x-private-request": "secret",
            "x-kept": "yes",
            "connection": "x-connection-token",
            "x-connection-token": "secret",
        },
    )
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["x-kept"] == "yes"
    assert response.headers["cache-control"] == "max-age=5"
    assert "set-cookie" not in response.headers
    assert "authentication-info" not in response.headers
    assert "x-private-response" not in response.headers
    forwarded = transport.requests[-1]
    assert forwarded.url == "https://upstream.test/api/orders/one"
    assert forwarded.query == b"x=1&x=%2f"
    headers = dict(forwarded.headers)
    assert headers[b"x-kept"] == b"yes"
    assert b"authorization" not in headers
    assert b"cookie" not in headers
    assert b"x-private-request" not in headers
    assert b"x-connection-token" not in headers


def test_owner_bearer_can_proxy_arbitrary_routes():
    client, transport = make_client()
    assert client.delete("/proxy/example/new/path", headers={"authorization": "Bearer owner-secret"}).status_code == 200
    assert transport.requests[-1].owner is True
    assert (
        client.delete("/proxy/example/new/path", headers={"authorization": "Bearer consumer-secret"}).status_code == 403
    )


def test_custom_method_and_no_retry():
    client, transport = make_client()
    response = client.request(
        "FROB", "/proxy/example/orders", headers={"authorization": "Bearer consumer-secret"}, content=b"payload"
    )
    assert response.status_code == 200
    assert len(transport.requests) == 1
    assert transport.requests[0].method == "FROB"
    assert transport.requests[0].body == b"payload"


def test_redirect_rewriting_and_unmapped_redirects():
    client, transport = make_client()
    transport.response = ProxyResponse(302, ((b"location", b"/api/orders/two?x=1#part"),), b"")
    response = client.get(
        "/proxy/example/orders/one", headers={"authorization": "Bearer consumer-secret"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/proxy/example/orders/two?x=1#part"
    assert len(transport.requests) == 1
    for location in (b"https://other.test/api/orders/two", b"/outside", b"/api/%2e%2e/outside"):
        transport.response = ProxyResponse(302, ((b"location", location),), b"")
        response = client.get(
            "/proxy/example/orders/one", headers={"authorization": "Bearer consumer-secret"}, follow_redirects=False
        )
        assert response.status_code == 502
        assert "location" not in response.headers


if __name__ == "__main__":
    pytest_bazel.main()
