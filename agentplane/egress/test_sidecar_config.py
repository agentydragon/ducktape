"""The sidecar authoring model keeps invalid hop settings out of rendered settings."""

import json

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.egress.sidecar_config import Proxy, Settings, WorkloadRoute


@pytest.mark.parametrize(
    "transport",
    [
        {"mode": "plaintext", "ca_file": "/ca", "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k"}},
        {"mode": "tls", "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k"}},
        {"mode": "tls", "ca_file": "relative", "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k"}},
        {"mode": "tls", "ca_file": "/ca", "auth": {"mode": "spiffe", "cert_file": "relative", "key_file": "/k"}},
        {"mode": "tls", "ca_file": "/ca", "auth": {"mode": "spiffe", "cert_file": "/c"}},
        {"mode": "tls", "ca_file": "/ca", "auth": {"mode": "token", "cert_file": "/c", "key_file": "/k"}},
        {
            "mode": "tls",
            "ca_file": "/ca",
            "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k", "token_file": "/token"},
        },
    ],
)
def test_rejects_invalid_proxy_transport(transport: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Proxy.model_validate_json(json.dumps({"address": "egress.example:8888", "transport": transport}))


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
        Proxy.model_validate_json(
            json.dumps(
                {
                    "address": address,
                    "transport": {
                        "mode": "tls",
                        "ca_file": "/ca",
                        "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k"},
                    },
                }
            )
        )


def test_workload_route_rejects_per_route_tls_credentials() -> None:
    with pytest.raises(ValidationError):
        WorkloadRoute.model_validate(
            {
                "upstream": "notifications:8443",
                "server_uri": "spiffe://agentplane/notifications",
                "tls": {"cert_file": "/cert", "key_file": "/key", "ca_file": "/ca"},
            }
        )


def test_retired_audience_token_settings_are_omitted_from_rendered_settings() -> None:
    settings = Settings(
        proxy=Proxy.model_validate(
            {
                "address": "egress.example:8888",
                "transport": {
                    "mode": "tls",
                    "ca_file": "/ca",
                    "auth": {"mode": "spiffe", "cert_file": "/c", "key_file": "/k"},
                },
            }
        ),
        audience_token_files={"kubernetes": "/var/run/agentplane-egress/kubernetes-token"},
    )

    assert "audience_token_files" not in settings.model_dump(mode="json", exclude_none=True)


if __name__ == "__main__":
    pytest_bazel.main()
