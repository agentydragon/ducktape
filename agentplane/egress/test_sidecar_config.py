"""The sidecar authoring union keeps invalid hop combinations out of rendered settings."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.egress.sidecar_config import Proxy


@pytest.mark.parametrize(
    "transport",
    [
        {"mode": "plaintext", "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k"}},
        {"mode": "tls", "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k"}},
        {"mode": "tls", "ca_file": "/ca", "auth": {"mode": "kubernetes_bearer"}},
        {"mode": "plaintext", "auth": {"mode": "kubernetes_bearer", "token_file": "relative"}},
        {"mode": "tls", "ca_file": "relative", "auth": {"mode": "kubernetes_bearer", "token_file": "/token"}},
        {
            "mode": "tls",
            "ca_file": "/ca",
            "auth": {"mode": "kubernetes_bearer", "token_file": "/token", "cert_file": "/c"},
        },
        {
            "mode": "tls",
            "ca_file": "/ca",
            "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k", "token_file": "/token"},
        },
    ],
)
def test_rejects_invalid_auth_transport_combinations(transport: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Proxy.model_validate({"address": "egress.example:8888", "transport": transport})


@pytest.mark.parametrize(
    "address",
    [
        "egress.example",
        "egress.example:0",
        "egress.example:65536",
        "2001:db8::1:8888",
        "[name]:8888",
        "egress.example:8888/path",
        "egress.example :8888",
        "egress.example:8888\t",
    ],
)
def test_proxy_address_requires_exact_host_port(address: str) -> None:
    with pytest.raises(ValidationError):
        Proxy.model_validate(
            {
                "address": address,
                "transport": {"mode": "plaintext", "auth": {"mode": "kubernetes_bearer", "token_file": "/token"}},
            }
        )


if __name__ == "__main__":
    pytest_bazel.main()
