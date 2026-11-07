"""Behavior of the gateway-to-provider HTTP boundary."""

import asyncio
import gzip

import httpx2
import pytest
import pytest_bazel
from pydantic import SecretStr

from platform_sessions.proxy import ProxyRequest
from platform_sessions.remote_transport import ProviderProtocolError, RemoteSessionTransport


def remote(handler):
    return RemoteSessionTransport(
        instance_id="shield",
        upstream_base_url="https://upstream.example/member",
        provider_base_url="https://provider.internal",
        owner_token=SecretStr("internal-owner"),
        route_token=SecretStr("internal-routes"),
        transport=httpx2.MockTransport(handler),
    )


def test_forwarding_preserves_bytes_and_uses_authenticated_provenance():
    payload = gzip.compress(b"%PDF-test")
    seen = []

    def provider(request):
        seen.append(request)
        assert request.url.raw_path == b"/internal/proxy/shield/letters/%C3%A9?x=%2F&x=2"
        assert request.headers["authorization"] == "Bearer internal-routes"
        assert request.headers.get_list("x-example") == ["first", "second"]
        assert "cookie" not in request.headers
        assert "x-platform-session-proxy-result" not in request.headers
        assert request.content == b"request\x00body"
        return httpx2.Response(
            401,
            headers=[
                (b"x-platform-session-proxy-result", b"1"),
                (b"content-encoding", b"gzip"),
                (b"x-example", b"one"),
                (b"x-example", b"two"),
            ],
            stream=httpx2.ByteStream(payload),
        )

    request = ProxyRequest(
        method="POST",
        url="https://upstream.example/member/letters/%C3%A9",
        query=b"x=%2F&x=2",
        headers=(
            (b"authorization", b"Bearer forged"),
            (b"cookie", b"forged"),
            (b"x-platform-session-proxy-result", b"1"),
            (b"x-example", b"first"),
            (b"x-example", b"second"),
        ),
        body=b"request\x00body",
    )
    result = asyncio.run(remote(provider).request(request))
    assert result.status == 401
    assert result.body == payload
    assert result.headers == ((b"content-encoding", b"gzip"), (b"x-example", b"one"), (b"x-example", b"two"))
    assert len(seen) == 1


def test_owner_identity_is_not_a_caller_supplied_header():
    def provider(request):
        assert request.headers["authorization"] == "Bearer internal-owner"
        assert "x-platform-session-proxy-result" not in request.headers
        return httpx2.Response(204, headers={"x-platform-session-proxy-result": "1"}, stream=httpx2.ByteStream(b""))

    request = ProxyRequest(
        "GET",
        "https://upstream.example/member/a",
        query=b"",
        headers=((b"x-platform-session-proxy-result", b"forged"),),
        body=b"",
        owner=True,
    )
    assert asyncio.run(remote(provider).request(request)).status == 204


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/member/a",
        "https://upstream.example/member-other/a",
        "https://upstream.example/member/%2Fsecret",
        "https://upstream.example/member/a?forged=1",
    ],
)
def test_invalid_upstream_target_is_rejected_without_dispatch(url):
    calls = []

    def provider(request):
        calls.append(request)
        raise AssertionError("must not dispatch")

    with pytest.raises(ValueError, match=r"configured upstream|outside|Ambiguous|Invalid"):
        asyncio.run(remote(provider).request(ProxyRequest("GET", url, b"", (), b"")))
    assert calls == []


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-platform-session-proxy-result", b"wrong")],
        [(b"x-platform-session-proxy-result", b"1"), (b"x-platform-session-proxy-result", b"1")],
    ],
)
def test_internal_auth_or_protocol_failure_is_not_confused_with_upstream_401(headers):
    with pytest.raises(ProviderProtocolError):
        asyncio.run(
            remote(lambda _: httpx2.Response(401, headers=headers, stream=httpx2.ByteStream(b""))).request(
                ProxyRequest("GET", "https://upstream.example/member/a", b"", (), b"")
            )
        )


def test_timeout_after_dispatch_is_not_retried():
    calls = []

    def provider(request):
        calls.append(request)
        raise httpx2.ReadTimeout("response was lost", request=request)

    with pytest.raises(httpx2.ReadTimeout):
        asyncio.run(remote(provider).request(ProxyRequest("GET", "https://upstream.example/member/a", b"", (), b"")))
    assert len(calls) == 1


if __name__ == "__main__":
    pytest_bazel.main()
