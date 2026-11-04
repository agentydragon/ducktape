"""TLS certificate material shared by browser ingress and OIDC test providers."""

import base64
import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


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
