"""Throwaway certificate authorities for the proxy tests.

Two distinct CAs stand in for the two trust relationships the deployed proxy has: the interception
CA, whose leaves the client sees and which the runner container trusts, and an upstream CA, which
signs the scripted upstream's certificate and which the proxy is told to trust for the far leg.
"""

from __future__ import annotations

import datetime
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class CertificateAuthority:
    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        return self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )


def _validity() -> tuple[datetime.datetime, datetime.datetime]:
    now = datetime.datetime.now(datetime.UTC)
    return now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=1)


def make_ca(common_name: str) -> CertificateAuthority:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    not_before, not_after = _validity()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        # mitmproxy's leaves name their issuer by this identifier; OpenSSL refuses a chain without it.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
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
        .sign(key, hashes.SHA256())
    )
    return CertificateAuthority(cert=cert, key=key)


def issue_leaf(ca: CertificateAuthority, hostname: str, directory: Path) -> tuple[Path, Path]:
    """A server certificate for `hostname` signed by `ca`, written as PEM files for a TLS server."""
    key = ec.generate_private_key(ec.SECP256R1())
    not_before, not_after = _validity()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca.key, hashes.SHA256())
    )
    cert_path = directory / f"{hostname}-cert.pem"
    key_path = directory / f"{hostname}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def write_ca(ca: CertificateAuthority, directory: Path, stem: str) -> tuple[Path, Path]:
    cert_path = directory / f"{stem}-cert.pem"
    key_path = directory / f"{stem}-key.pem"
    cert_path.write_bytes(ca.cert_pem)
    key_path.write_bytes(ca.key_pem)
    return cert_path, key_path


def server_tls_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))
    return context


def client_tls_context(ca: CertificateAuthority) -> ssl.SSLContext:
    """A client context trusting `ca` and nothing else."""
    return ssl.create_default_context(cadata=ca.cert_pem.decode())
