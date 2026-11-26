"""The Python deployment contract accepts the current service identity settings."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.action_service.settings import EnvironmentServicePeer, ServicePeer, Settings
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    CENTRAL_SAMPLER_URI,
    MCP_EDGE_URI,
    POLICY_CONTROLLER_URI,
    SANDBOX_CONTROLLER_URI,
)


def _settings_source() -> dict[str, object]:
    return {
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "tls": {"cert_file": "/etc/actions/cert", "key_file": "/etc/actions/key"},
            "auth": [{"mode": "spiffe", "ca_file": "/etc/actions/ca"}],
        },
        "policy_namespace": "test-agentplane",
        "caller_service_account_namespaces": ["test-agentplane"],
    }


def test_session_delegation_service_is_optional_and_only_trusts_the_central_sampler() -> None:
    source = _settings_source()
    settings = Settings.model_validate(source)
    assert settings.session_delegation_service == ""
    assert "session_delegation_service" not in settings.model_dump(mode="json")

    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        Settings.model_validate(
            source
            | {
                "rpc": {
                    "listen_address": ":8080",
                    "auth": [{"mode": "kubernetes_bearer", "audiences": ["test-actions"]}],
                }
            }
        )

    source["rpc"] = {
        "listen_address": "0.0.0.0:8080",
        "tls": {"cert_file": "/etc/actions/cert", "key_file": "/etc/actions/key"},
        "auth": [{"mode": "spiffe", "ca_file": "/etc/actions/ca"}],
    }
    settings = Settings.model_validate(source | {"session_delegation_service": CENTRAL_SAMPLER_URI})
    assert settings.session_delegation_service == CENTRAL_SAMPLER_URI
    assert settings.model_dump(mode="json")["session_delegation_service"] == CENTRAL_SAMPLER_URI

    with pytest.raises(ValidationError, match="session_delegation_service must be"):
        Settings.model_validate(source | {"session_delegation_service": "spiffe://agentplane/mcp-edge"})


def test_static_settings_do_not_accept_runtime_credentials() -> None:
    source = _settings_source()
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source | {"database_url": "postgresql://runtime-secret"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(
            source
            | {
                "web_push": {
                    "private_key_pem": "runtime-secret",
                    "subject": "mailto:operator@example.test",
                    "public_base_url": "https://app.example.test",
                    "allowed_push_hosts": ["push.example.test"],
                }
            }
        )


@pytest.mark.parametrize(
    "server_uri", ["spiffe://agentplane/session-dispatcher", "spiffe://agentplane/history-ingress"]
)
def test_service_peer_accepts_only_the_actions_svid(server_uri: str) -> None:
    tls = {
        "ca_file": "/trust/ca.pem",
        "server_uri": server_uri,
        "cert_file": "/svid/tls.crt",
        "key_file": "/svid/tls.key",
        "client_uri": "spiffe://agentplane/actions",
    }
    source = {"target": "peer:8443", "request_timeout_s": 10, "tls": tls}
    peer = ServicePeer.model_validate(source)
    assert peer.token_file == ""
    assert peer.tls is not None
    assert peer.tls.uses_svid
    with pytest.raises(ValidationError, match="cannot be combined"):
        ServicePeer.model_validate(source | {"token_file": "/token"})
    with pytest.raises(ValidationError, match="client_uri must be"):
        ServicePeer.model_validate(source | {"tls": tls | {"client_uri": "spiffe://agentplane/app"}})
    with pytest.raises(ValidationError, match="service SVID requires"):
        ServicePeer.model_validate(source | {"tls": tls | {"key_file": None}})


def test_environment_service_peer_is_optional_and_pins_controller_svid() -> None:
    source = _settings_source()
    settings = Settings.model_validate(source)
    assert settings.environment_service is None

    tls = {
        "ca_file": "/trust/ca.pem",
        "server_uri": SANDBOX_CONTROLLER_URI,
        "cert_file": "/svid/actions.crt",
        "key_file": "/svid/actions.key",
        "client_uri": ACTIONS_URI,
    }
    peer = EnvironmentServicePeer.model_validate(
        {"target": "sandbox-controller.agentplane.svc:8082", "request_timeout_s": 12, "tls": tls}
    )
    assert peer.tls is not None
    assert peer.tls.server_uri == SANDBOX_CONTROLLER_URI
    configured = Settings.model_validate(source | {"environment_service": peer.model_dump(mode="json")})
    assert configured.environment_service is not None
    assert configured.environment_service.target == "sandbox-controller.agentplane.svc:8082"

    for invalid in (
        {"request_timeout_s": 12, "tls": tls},
        {"target": "sandbox-controller.agentplane.svc:8082", "tls": tls},
        {
            "target": "sandbox-controller.agentplane.svc:8082",
            "request_timeout_s": 12,
            "tls": tls | {"server_uri": "spiffe://agentplane/session-dispatcher"},
        },
        {
            "target": "sandbox-controller.agentplane.svc:8082",
            "request_timeout_s": 12,
            "tls": tls | {"client_uri": "spiffe://agentplane/app"},
        },
        {"target": "sandbox-controller.agentplane.svc:8082", "request_timeout_s": 12, "tls": tls | {"key_file": None}},
        {
            "target": "sandbox-controller.agentplane.svc:8082",
            "request_timeout_s": 12,
            "token_file": "/token",
            "tls": {"ca_file": "/trust/ca.pem", "server_uri": SANDBOX_CONTROLLER_URI},
        },
    ):
        with pytest.raises(ValidationError):
            EnvironmentServicePeer.model_validate(invalid)


def _policy_service(*, target: str = "test-policy-controller:8443", client_uri: str = ACTIONS_URI) -> dict[str, object]:
    return {
        "target": target,
        "tls": {
            "ca_file": "/etc/actions/ca",
            "server_uri": POLICY_CONTROLLER_URI,
            "cert_file": "/etc/actions/cert",
            "key_file": "/etc/actions/key",
            "client_uri": client_uri,
        },
    }


def test_policy_service_is_optional_and_calls_as_actions() -> None:
    source = _settings_source()
    assert "policy_service" not in Settings.model_validate(source).model_dump(mode="json")
    settings = Settings.model_validate(source | {"policy_service": _policy_service()})
    assert settings.model_dump(mode="json")["policy_service"]["target"] == "test-policy-controller:8443"


@pytest.mark.parametrize(
    "peer",
    [
        _policy_service(target=""),
        _policy_service(client_uri=MCP_EDGE_URI),
        {**_policy_service(), "tls": {"ca_file": "/etc/actions/ca", "server_uri": ACTIONS_URI}},
        {
            "target": "test-policy-controller:8443",
            "tls": {"ca_file": "/etc/actions/ca", "server_uri": POLICY_CONTROLLER_URI},
        },
    ],
)
def test_policy_service_rejects_incomplete_or_wrong_peers(peer: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(_settings_source() | {"policy_service": peer})


if __name__ == "__main__":
    pytest_bazel.main()
