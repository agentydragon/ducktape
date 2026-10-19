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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from third_party.containers import nginx_unprivileged
from util.net import bind_free_port
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer

# HTTPX loads its optional HTTP/2 transport lazily.
# gazelle:include_dep @pypi//h2


@dataclass(frozen=True)
class BrowserCertificate:
    directory: Path
    spki: str


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
    return BrowserCertificate(directory, base64.b64encode(hashlib.sha256(public_key).digest()).decode())


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


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    # A reset is how one side ends a connection here, as an nginx reload does on purpose.
    except ConnectionError:
        pass
    finally:
        writer.close()


@asynccontextmanager
async def _forwarding(listener: socket.socket, upstream: Path) -> AsyncIterator[None]:
    """Accept on `listener` and relay each connection, bytes untouched, to the Unix socket `upstream`."""

    async def relay(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_unix_connection(upstream)
        # nginx is not listening yet: the client's TLS handshake fails and its readiness loop retries.
        except FileNotFoundError, ConnectionRefusedError:
            client_writer.close()
            return
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))

    async with await asyncio.start_server(relay, sock=listener):
        yield


@asynccontextmanager
async def http2_proxy(upstream: str, certificate: BrowserCertificate) -> AsyncIterator[Ingress]:
    await asyncio.to_thread(load_oci_image, nginx_unprivileged.IMAGE)
    # nginx in a host-network container can neither inherit a held socket nor bind port 0, so it listens on
    # a Unix socket in a directory of this test's own, and a held TCP port forwards to it. Under /tmp: the
    # test's TMPDIR can be longer than a socket path may be.
    sockets_directory = tempfile.TemporaryDirectory(prefix="http2-ingress-", dir="/tmp")
    sockets = Path(sockets_directory.name)
    (certificate.directory / "nginx.conf").write_text(
        dedent(f"""\
            pid /tmp/test-nginx.pid;
            error_log /dev/stderr info;
            # A reload's old workers close what they still hold at once.
            worker_shutdown_timeout 10ms;
            events {{ worker_connections 1024; }}
            http {{
                access_log /dev/stdout;
                client_body_temp_path /tmp/client_temp;
                proxy_temp_path /tmp/proxy_temp;
                server {{
                    listen unix:/sockets/nginx.sock ssl;
                    http2 on;
                    ssl_certificate /test/certificate.pem;
                    ssl_certificate_key /test/key.pem;
                    location / {{
                        proxy_pass {upstream};
                        proxy_http_version 1.1;
                        proxy_set_header Host $http_host;
                        proxy_set_header Connection "";
                        proxy_buffering off;
                        proxy_read_timeout 75s;
                    }}
                }}
            }}
            """)
    )
    container = (
        LoggedContainer(nginx_unprivileged.IMAGE.tag, test_name="conversation-http2-ingress")
        # Our uid, so this process may connect to the socket nginx creates; group 0, which the image's own
        # writable paths are granted to for running as an arbitrary user.
        .with_kwargs(network_mode="host", user=f"{os.getuid()}:0")
        .with_volume_mapping(str(certificate.directory), "/test", mode="ro")
        .with_volume_mapping(str(sockets), "/sockets", mode="rw")
        .with_command("nginx -c /test/nginx.conf -g 'daemon off;'")
    )
    listener = bind_free_port()
    url = f"https://127.0.0.1:{listener.getsockname()[1]}"
    context = ssl.create_default_context(cafile=str(certificate.directory / "certificate.pem"))
    with sockets_directory, container:
        async with _forwarding(listener, sockets / "nginx.sock"):
            async with asyncio.timeout(30), httpx.AsyncClient(verify=context, http2=True, timeout=2) as client:
                while True:
                    try:
                        response = await client.get(f"{url}/models")
                    # A slow first answer is not a failed start: the 30 s budget decides that.
                    except httpx.ConnectError, httpx.TimeoutException:
                        await asyncio.sleep(0.1)
                        continue
                    response.raise_for_status()
                    assert response.http_version == "HTTP/2"
                    break
            yield Ingress(url, container)
