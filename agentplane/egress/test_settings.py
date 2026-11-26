"""The proxy's required `policy_service` block reaches PolicyService as the egress proxy."""

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.egress.settings import Settings
from agentplane.identity.auth import listener
from agentplane.net.peertls.config import EGRESS_PROXY_URI, MCP_EDGE_URI, POLICY_CONTROLLER_URI


def _settings(**extra: object) -> Settings:
    return Settings.model_validate(
        {
            "rules_namespace": "test-rules",
            "credentials_namespace": "test-credentials",
            "allowed_service_account_namespaces": frozenset({"test-sandboxes"}),
            "forward": listener.Listener(
                listen_address="127.0.0.1:8888",
                tls=listener.TLS(cert_file=Path("/svid/tls.crt"), key_file=Path("/svid/tls.key")),
                auth=[listener.SPIFFEAuth(ca_file=Path("/svid/ca.crt"))],
            ),
            "ca_cert": Path("/ca.crt"),
            "ca_key": Path("/ca.key"),
            "log_levels": {},
            **extra,
        }
    )


def _peer(client_uri: str) -> dict[str, object]:
    return {
        "target": "policy-controller.test:8443",
        "tls": {
            "ca_file": "/svid/ca.crt",
            "server_uri": POLICY_CONTROLLER_URI,
            "cert_file": "/svid/tls.crt",
            "key_file": "/svid/tls.key",
            "client_uri": client_uri,
        },
    }


def test_policy_service_is_required() -> None:
    with pytest.raises(ValidationError):
        _settings()


def test_policy_service_accepts_the_proxy_as_caller() -> None:
    settings = _settings(policy_service=_peer(EGRESS_PROXY_URI))
    assert settings.model_dump(mode="json")["policy_service"]["tls"]["client_uri"] == EGRESS_PROXY_URI


def test_policy_service_rejects_another_caller() -> None:
    with pytest.raises(ValidationError):
        _settings(policy_service=_peer(MCP_EDGE_URI))


if __name__ == "__main__":
    pytest_bazel.main()
