"""Throwaway certificate authorities and the server certificates they sign, for tests.

The egress proxy tests use two CAs for the two trust relationships the deployed proxy has: the
interception CA, whose leaves the client sees and which the runner container trusts, and an upstream
CA, which signs the scripted upstream's certificate and which the proxy is told to trust for the far
leg. The k3s fixture signs Dex's serving certificate with one.
"""

from __future__ import annotations

import datetime
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

RUNNER_GATEWAY_TEST_DNS = "runner-gateway.test"


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
        # A leaf names its issuer by this identifier; OpenSSL refuses a chain without it.
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
        # Python's default context verifies strictly (`ssl.VERIFY_X509_STRICT`) and refuses a leaf without it.
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()), critical=False)
        .sign(ca.key, hashes.SHA256())
    )
    cert_path = directory / f"{hostname}-cert.pem"
    key_path = directory / f"{hostname}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def runner_identity_uri(namespace: str, sandbox_uid: str, credential_id: str) -> str:
    """Return the Runner identity URI used by the Gateway and transport fixtures."""
    if (
        not namespace
        or len(namespace) > 63
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in namespace)
    ):
        raise ValueError("namespace must be a lowercase DNS label")
    if namespace.startswith("-") or namespace.endswith("-"):
        raise ValueError("namespace must not start or end with a hyphen")
    for name, value in (("Sandbox UID", sandbox_uid), ("credential ID", credential_id)):
        if (
            not value
            or value in {".", ".."}
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                for character in value
            )
        ):
            raise ValueError(f"{name} must be a non-empty URI path segment")
    return f"spiffe://agentplane/runner/{namespace}/{sandbox_uid}/{credential_id}"


def issue_workload_leaf(
    ca: CertificateAuthority, uri: str, directory: Path, *, dns_name: str = RUNNER_GATEWAY_TEST_DNS
) -> tuple[Path, Path]:
    """Issue an mTLS leaf with one URI identity and the supplied test DNS name.

    Runner Gateway test servers need both SANs: Python clients verify the DNS
    name as part of TLS, while the Go Gateway authenticates Runner identities
    from the URI SAN. Both clientAuth and serverAuth EKUs permit either
    transport direction in the same fixture.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    not_before, not_after = _validity()
    cert = (
        x509.CertificateBuilder()
        # URI SAN is the workload identity; SPIFFE URIs commonly exceed the 64-byte CN limit.
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agentplane-runner")]))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri), x509.DNSName(dns_name)]), critical=False
        )
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca.key, hashes.SHA256())
    )
    cert_path = directory / "workload-cert.pem"
    key_path = directory / "workload-key.pem"
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
