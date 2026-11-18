"""The Python deployment contract holds MCP Identity policy and listener settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentplane.mcp_edge.settings import Settings


def _settings_source() -> dict[str, object]:
    return {
        "actions_target": "actions:8080",
        "policy_namespace": "test-policy",
        "rpc_tls": {
            "listen": "0.0.0.0:8443",
            "cert_file": Path("/etc/agentplane/tls/tls.crt"),
            "key_file": Path("/etc/agentplane/tls/tls.key"),
        },
        "operator_oidc": {
            "issuer": "https://operator.example",
            "audience": "test-edge",
            "jwks_uri": "https://operator.example/jwks",
        },
    }


def _oauth_settings() -> dict[str, object]:
    return {
        "upstream_client_id": "client",
        "upstream_client_secret_file": Path("/etc/agentplane-mcp/client-secret"),
        "base_url": "https://edge.example",
        "integration_app_url": "https://app.example",
        "upstream_issuer": "https://operator.example/issuer",
        "upstream_subject": "subject",
        "approving_operator": {"issuer": "https://operator.example", "subject": "operator"},
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


def test_tls_grpc_listener_is_required_and_plaintext_port_is_unknown() -> None:
    source = _settings_source()
    del source["rpc_tls"]
    with pytest.raises(ValidationError):
        Settings.model_validate(source)
    with pytest.raises(ValidationError):
        Settings.model_validate({**_settings_source(), "port": 8080})


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


def test_oauth_accepts_either_the_projected_token_or_exact_edge_svid() -> None:
    token = Settings.model_validate(
        {**_settings_source(), "actions_token_file": "/var/run/actions", "oauth": _oauth_settings()}
    )
    assert token.actions_token_file == "/var/run/actions"

    tls = {"ca_file": Path("/etc/agentplane/ca/ca.crt"), "server_uri": "spiffe://agentplane/actions"}
    server_tls = Settings.model_validate(
        {**_settings_source(), "actions_tls": tls, "actions_token_file": "/var/run/actions", "oauth": _oauth_settings()}
    )
    assert server_tls.actions_tls is not None
    assert not server_tls.actions_tls.uses_svid

    svid = Settings.model_validate(
        {
            **_settings_source(),
            "actions_tls": {
                **tls,
                "cert_file": Path("/var/run/secrets/agentplane-mcp/tls.crt"),
                "key_file": Path("/var/run/secrets/agentplane-mcp/tls.key"),
                "client_uri": "spiffe://agentplane/mcp-edge",
            },
            "oauth": _oauth_settings(),
        }
    )
    assert svid.actions_tls is not None
    assert svid.actions_tls.uses_svid
    assert svid.actions_token_file is None


def test_oauth_rejects_missing_mixed_or_wrong_action_caller_credentials() -> None:
    server_tls = {"ca_file": Path("/etc/agentplane/ca/ca.crt"), "server_uri": "spiffe://agentplane/actions"}
    tls = {
        "ca_file": Path("/etc/agentplane/ca/ca.crt"),
        "server_uri": "spiffe://agentplane/actions",
        "cert_file": Path("/var/run/secrets/agentplane-mcp/tls.crt"),
        "key_file": Path("/var/run/secrets/agentplane-mcp/tls.key"),
        "client_uri": "spiffe://agentplane/mcp-edge",
    }
    invalid = (
        {**_settings_source(), "oauth": _oauth_settings()},
        {**_settings_source(), "actions_tls": server_tls, "oauth": _oauth_settings()},
        {
            **_settings_source(),
            "actions_tls": tls,
            "actions_token_file": "/var/run/actions",
            "oauth": _oauth_settings(),
        },
        {
            **_settings_source(),
            "actions_tls": {**tls, "client_uri": "spiffe://agentplane/app"},
            "oauth": _oauth_settings(),
        },
    )
    for source in invalid:
        with pytest.raises(ValidationError):
            Settings.model_validate(source)


def test_oauth_file_also_requires_an_action_caller_credential() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**_settings_source(), "oauth_file": Path("/etc/agentplane-mcp/oauth.yaml")})
