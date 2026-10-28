"""The k3s fixture signer follows cert-manager Certificate.spec for real TLS identities."""

import pytest
import pytest_bazel
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from agentplane.system_tests.k3s.certificate_issuer import issue_certificate
from agentplane.testing.tls import make_ca


def test_leaf_uses_requested_uri_san_and_both_tls_ekus() -> None:
    root = make_ca("k3s-runner-root")
    uri = "spiffe://agentplane/runner/test-sandboxes/sandbox-uid/credential-id"
    cert_chain, key, leaf = issue_certificate(
        {
            "uris": [uri],
            "usages": ["digital signature", "key encipherment", "client auth", "server auth"],
            "privateKey": {"algorithm": "ECDSA", "size": 256},
            "duration": "24h",
        },
        root,
        root_certificate=root.cert,
    )

    assert x509.load_pem_x509_certificate(cert_chain).public_bytes(serialization.Encoding.PEM) == leaf.public_bytes(
        serialization.Encoding.PEM
    )
    assert len(cert_chain.split(b"-----END CERTIFICATE-----")) == 3
    assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.UniformResourceIdentifier
    ) == [uri]
    assert set(leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == {
        ExtendedKeyUsageOID.CLIENT_AUTH,
        ExtendedKeyUsageOID.SERVER_AUTH,
    }
    key_usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    assert key_usage.digital_signature
    assert key_usage.key_encipherment
    assert key


def test_ca_request_requires_signing_usages_and_marks_ca() -> None:
    root = make_ca("k3s-runner-root")
    _, _, intermediate = issue_certificate(
        {
            "isCA": True,
            "usages": ["cert sign", "crl sign"],
            "privateKey": {"algorithm": "ECDSA", "size": 256},
            "duration": "87600h",
        },
        root,
    )

    assert intermediate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    key_usage = intermediate.extensions.get_extension_for_class(x509.KeyUsage).value
    assert key_usage.key_cert_sign
    assert key_usage.crl_sign
    with pytest.raises(ValueError, match="cert sign"):
        issue_certificate({"isCA": True, "usages": ["crl sign"]}, root)


if __name__ == "__main__":
    pytest_bazel.main()
