"""TLS scaffolding shared by the interception suites (HTTP/2, WebSocket-over-TLS).

Both suites drive a real client through a CONNECT tunnel that mitmproxy intercepts: the client
trusts the runner's generated MITM CA on the client leg, and the runner is given ``ssl_insecure``
for the self-signed upstream on the far leg (a test seam — the behaviour under test is the gate's,
not mitmproxy's upstream-cert verification). Targeting ``localhost`` rather than an IP keeps SNI
present so the MITM leaf verifies by hostname.
"""

from __future__ import annotations

import datetime
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def make_self_signed_cert(common_name: str, directory: Path) -> tuple[Path, Path]:
    """A throwaway self-signed cert+key for a test upstream; the runner reaches it with ssl_insecure."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / f"{common_name}-cert.pem"
    key_path = directory / f"{common_name}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def server_tls_context(cert_path: Path, key_path: Path, alpn_protocols: list[str] | None = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))
    if alpn_protocols is not None:
        context.set_alpn_protocols(alpn_protocols)
    return context


def mitmproxy_ca_path(tmp_path: Path) -> Path:
    """The MITM CA the runner generates under its confdir, which the client leg must trust."""
    return tmp_path / "mitmproxy-confdir" / "mitmproxy-ca-cert.pem"


def client_tls_context(tmp_path: Path) -> ssl.SSLContext:
    """A client-side context trusting the runner's MITM CA (and nothing else)."""
    return ssl.create_default_context(cafile=str(mitmproxy_ca_path(tmp_path)))
