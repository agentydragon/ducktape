from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from mitmproxy import certs


@dataclass
class Certificates:
    cert: Path
    key: Path
    ca: Path


def certificates(directory: Path, label: str, hostname: str | None) -> Certificates:
    key, authority = certs.create_ca(f"test-{label}", f"test-{label}", 2048)
    leaf = (
        certs.dummy_cert(key, authority, hostname, [x509.DNSName(hostname)]).to_cryptography()
        if hostname
        else authority
    )
    paths = Certificates(directory / f"{label}.crt", directory / f"{label}.key", directory / f"{label}-ca.crt")
    paths.cert.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths.ca.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    paths.key.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return paths
