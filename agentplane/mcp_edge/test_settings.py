"""The Python deployment contract holds MCP Identity policy and listener settings."""

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.mcp_edge.settings import Settings


def _settings_source() -> dict[str, object]:
    return {
        "actions_target": "actions:8080",
        "policy_namespace": "test-policy",
        "operator_oidc": {
            "issuer": "https://operator.example",
            "audience": "test-edge",
            "jwks_uri": "https://operator.example/jwks",
        },
    }


def test_identity_settings_are_typed() -> None:
    source = _settings_source()
    settings = Settings.model_validate(source)
    assert settings.policy_namespace == "test-policy"
    assert settings.actions_token_file is None
    assert "operator_services" not in settings.model_dump(mode="json")

    source["actions_token_file"] = "/var/run/actions"
    settings = Settings.model_validate(source)
    assert settings.policy_namespace == "test-policy"
    assert settings.actions_token_file == "/var/run/actions"


def test_operator_services_require_the_additional_mutual_tls_listener() -> None:
    rpc_tls = {
        "listen": "0.0.0.0:8082",
        "cert_file": Path("/etc/agentplane/tls/tls.crt"),
        "key_file": Path("/etc/agentplane/tls/tls.key"),
        "client_ca_file": Path("/etc/agentplane/tls/client-ca.crt"),
    }
    settings = Settings.model_validate(
        {**_settings_source(), "rpc_tls": rpc_tls, "operator_services": ["spiffe://agentplane/app"]}
    )
    assert settings.rpc_tls is not None
    assert settings.operator_services == ["spiffe://agentplane/app"]

    for values in (
        {"operator_services": ["spiffe://agentplane/app"]},
        {
            "rpc_tls": {key: value for key, value in rpc_tls.items() if key != "client_ca_file"},
            "operator_services": ["spiffe://agentplane/app"],
        },
        {"rpc_tls": rpc_tls},
        {"rpc_tls": rpc_tls, "operator_services": ["spiffe://agentplane/not-a-service"]},
        {"rpc_tls": rpc_tls, "operator_services": ["spiffe://agentplane/app", "spiffe://agentplane/app"]},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**_settings_source(), **values})


if __name__ == "__main__":
    pytest_bazel.main()
