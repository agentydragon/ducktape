"""Real Squid: nested TLS, parent auth, and absence of direct fallback.

The synthetic parent terminates only the outer TLS layer. The origin's distinct
certificate must reach the client unchanged through Squid and the parent tunnel.
"""

import base64
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path

import docker
import pytest
import pytest_bazel
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_fixed

from devinfra.github_api_capture.relay_config import render
from util.bazel import runfiles
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

SQUID = OciImage("_main/devinfra/github_api_capture/squid_image.rloc", "github-api-relay-squid:test")
USERNAME = "test-relay-client"
PASSWORD = "0123456789abcdef" * 4
AUTHORIZATION = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()


def certificate(directory: Path, name: str, *, wrong_name: bool = False) -> Path:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name}-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("wrong.test")] if wrong_name else [x509.IPAddress(IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = directory / f"{name}.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.with_suffix(".ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    pem.with_suffix(".key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return pem


@contextmanager
def tls_server(handler: type[BaseHTTPRequestHandler], cert: Path) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, cert.with_suffix(".key"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def squid_image() -> str:
    image = load_oci_image(SQUID)
    with closing(docker.from_env()) as client:
        version = client.containers.run(image, entrypoint=["/usr/sbin/squid-openssl"], command=["-v"], remove=True)
    (undeclared_outputs_dir() / "squid-version.log").write_bytes(version)
    assert b"--with-openssl" in version
    return image


@pytest.fixture
def private_credentials(tmp_path: Path) -> Path:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({USERNAME: PASSWORD}))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("failure", [None, "password", "untrusted", "hostname", "unavailable"])
def test_parent_transport(
    tmp_path: Path, squid_image: str, private_credentials: Path, failure: str | None, request: pytest.FixtureRequest
) -> None:
    origin_requests = []
    proxy_requests = []

    class Origin(QuietHandler):
        def do_GET(self) -> None:
            origin_requests.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic-origin")

    class Parent(QuietHandler):
        def authenticated(self) -> bool:
            proxy_requests.append(self.headers.get("Proxy-Authorization"))
            if proxy_requests[-1] == AUTHORIZATION:
                return True
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="synthetic-parent"')
            self.end_headers()
            return False

        def do_GET(self) -> None:
            if self.authenticated():
                assert self.path == "http://mitm.it/"
                self.send_response(200)
                self.end_headers()

        def do_CONNECT(self) -> None:
            if not self.authenticated():
                return
            assert self.path == f"127.0.0.1:{origin.server_port}"
            with socket.create_connection(("127.0.0.1", origin.server_port), timeout=5) as upstream:
                self.send_response(200)
                self.end_headers()
                while True:
                    readable, _, _ = select.select([self.connection, upstream], [], [], 5)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(data)

    origin_cert = certificate(tmp_path, "origin")
    parent_cert = certificate(tmp_path, "parent", wrong_name=failure == "hostname")
    trust = (certificate(tmp_path, "unrelated") if failure == "untrusted" else parent_cert).with_suffix(".ca.pem")
    if failure == "password":
        private_credentials.write_text(json.dumps({USERNAME: "a" * 64}))

    with ExitStack() as stack:
        origin = stack.enter_context(tls_server(Origin, origin_cert))
        parent = stack.enter_context(tls_server(Parent, parent_cert))
        # Reserve the listener until just before Squid starts; no sleeps for readiness.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            listen_port = reservation.getsockname()[1]
        parent_port = parent.server_port
        if failure == "unavailable":
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                parent_port = reservation.getsockname()[1]
        config = tmp_path / "squid.conf"
        config.write_text(
            render(
                host="127.0.0.1",
                port=parent_port,
                listen_port=listen_port,
                credentials_file=private_credentials,
                ca_bundle=trust,
            )
        )
        config.chmod(0o600)
        client = docker.from_env()
        stack.callback(client.close)
        container = client.containers.run(
            squid_image,
            entrypoint=["/usr/sbin/squid-openssl"],
            command=["-N", "-f", str(config)],
            detach=True,
            user=f"{tmp_path.stat().st_uid}:{tmp_path.stat().st_gid}",
            network_mode="host",
            volumes={str(tmp_path): {"bind": str(tmp_path), "mode": "ro"}},
        )
        stack.callback(container.remove, force=True)

        @retry(stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(OSError), reraise=True)
        def ready() -> None:
            with socket.create_connection(("127.0.0.1", listen_port), timeout=1):
                pass

        try:
            ready()
            connection = HTTPSConnection(
                "127.0.0.1",
                listen_port,
                timeout=10,
                context=ssl.create_default_context(cafile=str(origin_cert.with_suffix(".ca.pem"))),
            )
            stack.callback(connection.close)
            connection.set_tunnel("127.0.0.1", origin.server_port)
            if failure:
                with pytest.raises(OSError, match="Tunnel connection failed"):
                    connection.request("GET", "/")
                assert not origin_requests
            else:
                connection.request("GET", "/")
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == b"synthetic-origin"
                assert len(origin_requests) == 1
                assert "Proxy-Authorization" not in origin_requests[0]
                assert proxy_requests == [AUTHORIZATION]
                health = HTTPConnection("127.0.0.1", listen_port, timeout=10)
                stack.callback(health.close)
                health.request("GET", "http://mitm.it/")
                assert health.getresponse().status == 200
                assert proxy_requests == [AUTHORIZATION, AUTHORIZATION]
                assert len(origin_requests) == 1
        finally:
            logs = container.logs()
            (undeclared_outputs_dir() / f"{request.node.name}.log").write_bytes(logs)
            assert PASSWORD.encode() not in logs
            assert AUTHORIZATION.encode() not in logs


@pytest.mark.parametrize("value", [{}, {USERNAME: "bad\ncache_peer attacker.test parent 80 0"}, {"bad user": PASSWORD}])
def test_malformed_credentials_fail_closed(tmp_path: Path, value: dict[str, str]) -> None:
    credentials = tmp_path / "invalid.json"
    credentials.write_text(json.dumps(value))
    credentials.chmod(0o600)
    with pytest.raises(ValueError, match=r"Expected|Invalid"):
        render(host="proxy.test", port=443, listen_port=8788, credentials_file=credentials, ca_bundle=tmp_path / "ca")


def test_missing_credentials_fail_closed(tmp_path: Path) -> None:
    output = tmp_path / "squid.conf"
    result = subprocess.run(
        [
            sys.executable,
            str(runfiles.get_required_path("_main/devinfra/github_api_capture/relay_config.py")),
            "--host",
            "proxy.test",
            "--port",
            "443",
            "--listen-port",
            "8788",
            "--credentials-file",
            str(tmp_path / "absent"),
            "--ca-bundle",
            str(tmp_path / "ca"),
            "--output",
            str(output),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == b"github-api-relay: could not prepare private configuration\n"
    assert not result.stdout
    assert not output.exists()


def test_pinned_ca_replaces_old_bundle_and_nss(tmp_path: Path, squid_image: str) -> None:
    old_ca = certificate(tmp_path, "old-ca").with_suffix(".ca.pem")
    new_ca = certificate(tmp_path, "new-ca").with_suffix(".ca.pem")
    system_ca = certificate(tmp_path, "system-ca").with_suffix(".ca.pem")
    os.utime(new_ca, (1, 1))
    state = tmp_path / "state"
    nss = state / "nss"
    script = runfiles.get_required_path("_main/devinfra/github_api_capture/prepare_trust.sh")
    with closing(docker.from_env()) as client:
        for ca in (old_ca, new_ca):
            client.containers.run(
                squid_image,
                entrypoint=["/bin/bash"],
                command=["/prepare-trust.sh", str(ca), str(system_ca), str(state), str(nss)],
                volumes={
                    str(tmp_path): {"bind": str(tmp_path), "mode": "rw"},
                    str(script): {"bind": "/prepare-trust.sh", "mode": "ro"},
                },
                remove=True,
            )
        installed = client.containers.run(
            squid_image,
            entrypoint=["/usr/bin/certutil"],
            command=["-L", "-d", f"sql:{nss}", "-n", "ducktape-github-api-proxy", "-a"],
            volumes={str(tmp_path): {"bind": str(tmp_path), "mode": "ro"}},
            remove=True,
        )
    assert x509.load_pem_x509_certificate(installed) == x509.load_pem_x509_certificate(new_ca.read_bytes())
    bundle = state / "ca-bundle.pem"
    assert bundle.read_bytes() == system_ca.read_bytes() + new_ca.read_bytes()
    assert bundle.stat().st_mode & 0o077 == 0


def test_public_credentials_fail_closed(private_credentials: Path, tmp_path: Path) -> None:
    private_credentials.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        render(
            host="proxy.test",
            port=443,
            listen_port=8788,
            credentials_file=private_credentials,
            ca_bundle=tmp_path / "ca",
        )


if __name__ == "__main__":
    pytest_bazel.main()
