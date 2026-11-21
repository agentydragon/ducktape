"""Verified TLS on native gRPC channels, including loopback port-forward targets."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import grpc
import pytest
import pytest_bazel
from grpc_health.v1 import health_pb2, health_pb2_grpc

from agentplane.grpc.calls import dial
from agentplane.grpc.serving import add_health
from agentplane.testing.tls import issue_leaf, make_ca


@dataclass(frozen=True)
class TLSServer:
    port: int
    credentials: grpc.ChannelCredentials


@pytest.fixture
async def tls_server(tmp_path: Path) -> AsyncIterator[TLSServer]:
    ca = make_ca("test gRPC CA")
    certificate, key = issue_leaf(ca, "localhost", tmp_path)
    server = grpc.aio.server()
    add_health(server)
    port = server.add_secure_port(
        "127.0.0.1:0", grpc.ssl_server_credentials([(key.read_bytes(), certificate.read_bytes())])
    )
    await server.start()
    try:
        yield TLSServer(port, grpc.ssl_channel_credentials(root_certificates=ca.cert_pem))
    finally:
        await server.stop(grace=0)


@pytest.mark.parametrize("port_forward", [False, True])
async def test_tls_verifies_hostname_for_direct_and_port_forward_calls(
    tls_server: TLSServer, port_forward: bool
) -> None:
    target = f"127.0.0.1:{tls_server.port}" if port_forward else f"localhost:{tls_server.port}"
    async with dial(
        target, channel_credentials=tls_server.credentials, server_name="localhost" if port_forward else None
    ) as channel:
        response = await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest(), timeout=3)
    assert response.status == health_pb2.HealthCheckResponse.SERVING


@pytest.mark.parametrize("failure", ["hostname", "ca", "plaintext"])
async def test_tls_rejects_wrong_identity_and_untrusted_roots(tls_server: TLSServer, failure: str) -> None:
    credentials = tls_server.credentials
    if failure == "ca":
        credentials = grpc.ssl_channel_credentials(root_certificates=make_ca("untrusted test CA").cert_pem)
    async with dial(
        f"127.0.0.1:{tls_server.port}",
        channel_credentials=None if failure == "plaintext" else credentials,
        server_name=None if failure == "plaintext" else ("wrong.test" if failure == "hostname" else "localhost"),
    ) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest(), timeout=3)
    assert refused.value.code() == grpc.StatusCode.UNAVAILABLE


@pytest.mark.parametrize(
    "server_name",
    [
        "",
        "spiffe://agentplane/session-dispatcher",
        "https://localhost",
        "localhost:443",
        "*.test",
        " localhost",
        "127.0.0.1",
    ],
)
def test_override_requires_a_dns_name_and_tls_credentials(server_name: str) -> None:
    with pytest.raises(ValueError, match="server_name"):
        dial("127.0.0.1:12345", channel_credentials=grpc.ssl_channel_credentials(), server_name=server_name)


def test_override_cannot_be_ignored_on_plaintext_channels() -> None:
    with pytest.raises(ValueError, match="credentials"):
        dial("127.0.0.1:12345", server_name="localhost")


if __name__ == "__main__":
    pytest_bazel.main()
