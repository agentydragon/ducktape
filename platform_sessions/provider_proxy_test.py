"""Exercise the authenticated private proxy and the gateway-to-provider hop."""

from pathlib import PurePosixPath

import pytest_bazel
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from platform_sessions.auth import Bearer, BearerAuth
from platform_sessions.provider_proxy import ProviderProxy
from platform_sessions.proxy import HeaderPolicy, ProxyInstance, ProxyRequest, ProxyResponse
from platform_sessions.registry import IntegrationInstance
from platform_sessions.routes import Route


class Settings(BaseModel):
    pass


class Transport:
    def __init__(self):
        self.requests: list[ProxyRequest] = []
        self.response = ProxyResponse(
            401,
            ((b"set-cookie", b"secret=rotated"), (b"x-kept", b"yes"), (b"x-platform-session-proxy-result", b"forged")),
            b"upstream",
        )
        self.fail = False

    async def request(self, request: ProxyRequest) -> ProxyResponse:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("must not expose secret")
        return self.response


def provider():
    config = IntegrationInstance(
        "example", "fake", True, "https://upstream.test/api", PurePosixPath("/data/example"), 900, Settings()
    )
    local = Transport()
    policy = HeaderPolicy(request={"X-Private-Request"}, response={"X-Private-Response"})
    auth = BearerAuth(
        [
            Bearer(SecretStr("route-secret"), routes={"example": (Route("GET", "/orders/*"),)}),
            Bearer(SecretStr("owner-secret"), owner=True),
        ]
    )
    return ProviderProxy({"example": ProxyInstance(config, local, policy)}, auth), local


def test_private_endpoint_authenticates_and_enforces_routes():
    app, local = provider()
    client = TestClient(app)
    assert client.get("/internal/proxy/example/orders/1").status_code == 401
    response = client.get("/internal/proxy/example/orders/1", headers={"authorization": "Bearer bad"})
    assert response.status_code == 401
    assert "x-platform-session-proxy-result" not in response.headers
    response = client.post("/internal/proxy/example/orders/1", headers={"authorization": "Bearer route-secret"})
    assert response.status_code == 403
    assert "x-platform-session-proxy-result" not in response.headers
    assert not local.requests

    response = client.get(
        "/internal/proxy/example/orders/1?x=1&x=%2f",
        headers={
            "authorization": "Bearer route-secret",
            "cookie": "forged",
            "x-private-request": "secret",
            "x-platform-session-proxy-result": "forged",
        },
    )
    assert response.status_code == 401  # The upstream's response, after successful private authentication.
    assert response.headers.get_list("x-platform-session-proxy-result") == ["1"]
    assert "set-cookie" not in response.headers
    assert response.headers["x-kept"] == "yes"
    assert response.content == b"upstream"
    request = local.requests[-1]
    assert request.url == "https://upstream.test/api/orders/1"
    assert request.query == b"x=1&x=%2f"
    assert not request.owner
    assert (
        not {b"cookie", b"authorization", b"x-private-request", b"x-platform-session-proxy-result"}
        & dict(request.headers).keys()
    )

    response = client.delete("/internal/proxy/example/other", headers={"authorization": "Bearer owner-secret"})
    assert response.status_code == 401
    assert local.requests[-1].owner
    local.fail = True
    response = client.get("/internal/proxy/example/orders/1", headers={"authorization": "Bearer route-secret"})
    assert response.status_code == 502
    assert "x-platform-session-proxy-result" not in response.headers
    assert "secret" not in response.text
    assert len(local.requests) == 3


def test_private_endpoint_normalizes_redirects_to_provider_paths():
    app, local = provider()
    client = TestClient(app)
    auth = {"authorization": "Bearer route-secret"}
    for location, expected in (
        (b"https://upstream.test/api/orders/2?x=1", "/orders/2?x=1"),
        (b"../orders/2", "/orders/2"),
        (b"?page=2", "/orders/1?page=2"),
    ):
        local.response = ProxyResponse(302, ((b"location", location),), b"")
        response = client.get("/internal/proxy/example/orders/1", headers=auth, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == expected

    for location in (b"https://evil.test/api/orders/2", b"/outside", b"/api/%2fescape"):
        local.response = ProxyResponse(302, ((b"location", location),), b"")
        response = client.get("/internal/proxy/example/orders/1", headers=auth, follow_redirects=False)
        assert response.status_code == 502
        assert "location" not in response.headers


if __name__ == "__main__":
    pytest_bazel.main()
