"""TLS/HTTP2 browser ingress for concurrent Electric shape requests in acceptance tests."""

import asyncio
import base64
import hashlib
import ipaddress
import os
import socket
import ssl
import tempfile
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from jinja2 import Template

from agentplane.app.frontend.serving.testing.image import IMAGE as STATIC_IMAGE
from third_party.containers import nginx_unprivileged
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.tcp_relay import relay_tcp

# HTTPX loads its optional HTTP/2 transport lazily.
# gazelle:include_dep @pypi//h2


@dataclass(frozen=True)
class BrowserCertificate:
    directory: Path
    spki: str
    port: int
    port_reservation: socket.socket

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"


def browser_certificate(directory: Path) -> BrowserCertificate:
    directory.mkdir(mode=0o755)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-browser-ingress")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    (directory / "certificate.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    public_key = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    return BrowserCertificate(
        directory,
        base64.b64encode(hashlib.sha256(public_key).digest()).decode(),
        reservation.getsockname()[1],
        reservation,
    )


@dataclass(frozen=True)
class Ingress:
    url: str
    container: LoggedContainer

    async def drop_connections(self) -> None:
        """End every open connection, as a network drop does, while still accepting new ones.

        Browser offline emulation fails requests still waiting for a response, but not one
        already streaming, such as a live shape's SSE response.
        """
        exit_code, output = await asyncio.to_thread(self.container.exec, "nginx -c /test/nginx.conf -s reload")
        assert exit_code == 0, output


async def _wait_for_frontend(upstream: str) -> None:
    async with asyncio.timeout(30), httpx.AsyncClient(timeout=2) as client:
        while True:
            try:
                response = await client.get(f"{upstream}/healthz")
            # Docker can publish the port before nginx can answer: connections may be
            # refused, reset, or closed before HTTP response headers arrive.
            except httpx.TransportError:
                await asyncio.sleep(0.1)
                continue
            response.raise_for_status()
            return


@asynccontextmanager
async def http2_proxy(upstream: str, certificate: BrowserCertificate) -> AsyncIterator[Ingress]:
    await asyncio.to_thread(load_oci_image, nginx_unprivileged.IMAGE)
    await asyncio.to_thread(load_oci_image, STATIC_IMAGE)
    with ExitStack() as stack:
        static_container = stack.enter_context(
            LoggedContainer(STATIC_IMAGE.tag, test_name="conversation-static-frontend").with_exposed_ports(8080)
        )
        static_upstream = f"http://{static_container.get_container_host_ip()}:{static_container.get_exposed_port(8080)}"
        await _wait_for_frontend(static_upstream)
        # A separate nginx serves the actual built frontend. The TLS ingress supplies the
        # browser's single origin while API traffic still reaches the real app process.
        # nginx needs host networking to reach loopback app upstreams. Its UDS avoids
        # an unowned TCP port; the relay preserves TLS and HTTP/2 bytes unchanged.
        sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="http2-ingress-", dir="/tmp")))
        # Keep the endpoint already used to configure the app's public URL.
        listener = stack.enter_context(certificate.port_reservation)
        listener.listen()
        template = get_required_path("_main/agentplane/app/testing/http2_proxy.conf.j2").read_text()
        (certificate.directory / "nginx.conf").write_text(
            Template(template).render(upstream=upstream, static_upstream=static_upstream) + "\n"
        )
        container = (
            LoggedContainer(nginx_unprivileged.IMAGE.tag, test_name="conversation-http2-ingress")
            .with_kwargs(network_mode="host", user=f"{os.getuid()}:0")
            .with_volume_mapping(str(sockets), "/sockets", mode="rw")
            .with_volume_mapping(str(certificate.directory), "/test", mode="ro")
            .with_command("nginx -c /test/nginx.conf -g 'daemon off;'")
        )
        url = certificate.url
        context = ssl.create_default_context(cafile=str(certificate.directory / "certificate.pem"))
        with container:
            async with relay_tcp(listener, sockets / "nginx.sock"):
                async with asyncio.timeout(30), httpx.AsyncClient(verify=context, http2=True, timeout=2) as client:
                    while True:
                        try:
                            response = await client.get(f"{url}/readyz")
                        # A slow first answer is not a failed start: the 30 s budget decides that.
                        except httpx.TransportError:
                            await asyncio.sleep(0.1)
                            continue
                        response.raise_for_status()
                        assert response.http_version == "HTTP/2"
                        break
                yield Ingress(url, container)
