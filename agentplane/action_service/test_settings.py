"""The Python deployment contract accepts optional Connection delegation settings."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.action_service.settings import ServicePeer, Settings
from agentplane.net.peertls.config import CENTRAL_SAMPLER_URI


def _settings_source() -> dict[str, object]:
    return {
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "auth": [{"mode": "kubernetes_bearer", "audiences": ["test-actions"]}],
        },
        "policy_namespace": "test-agentplane",
        "caller_service_account_namespaces": ["test-agentplane"],
    }


def test_connection_delegation_actor_is_optional_and_accepted() -> None:
    source = _settings_source()
    settings = Settings.model_validate(source)
    assert settings.connection_delegation_actor is None
    rendered = settings.model_dump(mode="json")
    assert "service_readers" not in rendered
    assert "operator_services" not in rendered
    assert "connection_delegation_service" not in rendered

    source["connection_delegation_actor"] = {"namespace": "test-agentplane", "name": "mcp-edge"}
    settings = Settings.model_validate(source)
    assert settings.connection_delegation_actor is not None
    assert settings.connection_delegation_actor.namespace == "test-agentplane"
    assert settings.connection_delegation_actor.name == "mcp-edge"


def test_session_delegation_service_is_optional_and_only_trusts_the_central_sampler() -> None:
    source = _settings_source()
    settings = Settings.model_validate(source)
    assert settings.session_delegation_service == ""
    assert "session_delegation_service" not in settings.model_dump(mode="json")

    with pytest.raises(ValidationError, match="service callers require rpc spiffe client trust"):
        Settings.model_validate(source | {"session_delegation_service": CENTRAL_SAMPLER_URI})

    source["rpc"] = {
        "listen_address": "0.0.0.0:8080",
        "tls": {"cert_file": "/etc/actions/cert", "key_file": "/etc/actions/key"},
        "auth": [
            {"mode": "kubernetes_bearer", "audiences": ["test-actions"]},
            {"mode": "spiffe", "ca_file": "/etc/actions/ca"},
        ],
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


if __name__ == "__main__":
    pytest_bazel.main()
