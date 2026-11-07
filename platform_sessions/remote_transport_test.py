"""Behavior of the gateway-to-provider HTTP boundary."""

import asyncio
import datetime
import gzip
import http.server
import ipaddress
import ssl
import threading

import httpx2
import pytest
import pytest_bazel
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
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


def test_internal_bearer_cannot_be_sent_over_plaintext():
    with pytest.raises(ValueError, match="must use HTTPS"):
        RemoteSessionTransport(
            instance_id="shield",
            upstream_base_url="https://upstream.example/member",
            provider_base_url="http://provider.internal",
            owner_token=SecretStr("internal-owner"),
            route_token=SecretStr("internal-routes"),
        )


def test_provider_tls_context_must_authenticate_server():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="must verify"):
        RemoteSessionTransport(
            instance_id="shield",
            upstream_base_url="https://upstream.example/member",
            provider_base_url="https://provider.internal",
            owner_token=SecretStr("internal-owner"),
            route_token=SecretStr("internal-routes"),
            tls_context=context,
        )


def test_provider_https_verifies_custom_ca(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "provider.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200)
            self.send_header("x-platform-session-proxy-result", "1")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"https://127.0.0.1:{server.server_port}"

        def client(context: ssl.SSLContext | None = None) -> RemoteSessionTransport:
            return RemoteSessionTransport(
                instance_id="shield",
                upstream_base_url="https://upstream.example/member",
                provider_base_url=origin,
                owner_token=SecretStr("internal-owner"),
                route_token=SecretStr("internal-routes"),
                tls_context=context,
            )

        request = ProxyRequest("GET", "https://upstream.example/member/a", b"", (), b"")
        with pytest.raises(httpx2.ConnectError):
            asyncio.run(client().request(request))
        context = ssl.create_default_context(cadata=cert_pem.decode("ascii"))
        result = asyncio.run(client(context).request(request))
        assert result.status == 200
        assert result.body == b"ok"
        assert calls == ["/internal/proxy/shield/a"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


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
