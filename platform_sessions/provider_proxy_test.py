"""Exercise the authenticated private proxy and the gateway-to-provider hop."""

import asyncio
from pathlib import PurePosixPath

import httpx2
import pytest_bazel
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from platform_sessions.auth import Bearer, BearerAuth
from platform_sessions.provider_proxy import ProviderProxy
from platform_sessions.proxy import HeaderPolicy, ProxyInstance, ProxyRequest, ProxyResponse
from platform_sessions.registry import IntegrationInstance
from platform_sessions.remote_transport import RemoteSessionTransport
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


def test_gateway_and_provider_hop_preserve_bytes_and_identity():
    app, local = provider()
    local.response = ProxyResponse(
        403, ((b"location", b"/api/orders/2"), (b"x-duplicate", b"one"), (b"x-duplicate", b"two")), b"\x00\xff"
    )

    # Connect the synchronous HTTP client to the ASGI server in memory, preserving raw ASGI headers and body.
    def bridge(request: httpx2.Request) -> httpx2.Response:
        async def invoke():
            sent = []
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": request.method,
                "scheme": "https",
                "path": request.url.path,
                "raw_path": request.url.raw_path.split(b"?", 1)[0],
                "query_string": request.url.query,
                "headers": request.headers.raw,
                "client": ("test", 1),
                "server": ("provider.internal", 443),
                "root_path": "",
            }

            async def receive():
                return {"type": "http.request", "body": request.content, "more_body": False}

            async def send(message):
                sent.append(message)

            await app(scope, receive, send)
            return sent

        messages = asyncio.run(invoke())
        start, body = messages
        return httpx2.Response(start["status"], headers=start["headers"], stream=httpx2.ByteStream(body["body"]))

    remote = RemoteSessionTransport(
        instance_id="example",
        upstream_base_url="https://upstream.test/api",
        provider_base_url="https://provider.internal",
        owner_token=SecretStr("owner-secret"),
        route_token=SecretStr("route-secret"),
        transport=httpx2.MockTransport(bridge),
    )
    result = asyncio.run(remote.request(ProxyRequest("GET", "https://upstream.test/api/orders/1", b"x=%2f", (), b"")))
    assert result.status == 403
    assert result.body == b"\x00\xff"
    assert (b"location", b"/api/orders/2") in result.headers
    assert [v for k, v in result.headers if k == b"x-duplicate"] == [b"one", b"two"]
    assert local.requests[-1].query == b"x=%2f"
    assert not local.requests[-1].owner

    result = asyncio.run(
        remote.request(ProxyRequest("DELETE", "https://upstream.test/api/other", b"", (), b"", owner=True))
    )
    assert result.status == 403
    assert local.requests[-1].owner
    assert len(local.requests) == 2


if __name__ == "__main__":
    pytest_bazel.main()
