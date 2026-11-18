"""Dispatcher settings describe only the live receiver and its clients."""

from typing import cast

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.session_dispatcher.settings import Settings


def _settings(*, kubernetes_client_qps: float = 0) -> Settings:
    return cast(
        Settings,
        Settings.model_validate(
            {
                "sandbox_namespace": "test-sandboxes",
                "policy_namespace": "test-policies",
                "runner_token_file": "/run/test/token",
                "gateway_tls": {"ca_file": "/test/workload-ca", "server_uri": "spiffe://agentplane/session-dispatcher"},
                "rpc": {
                    "listen_address": ":8080",
                    "tls": {"cert_file": "/test/workload-cert", "key_file": "/test/workload-key"},
                    "auth": [{"mode": "spiffe", "ca_file": "/test/workload-ca"}],
                },
                "kubernetes_client_qps": kubernetes_client_qps,
            }
        ),
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("runner_security", {"mode": "insecure"}),
        ("runtime_proxy_readers", ["spiffe://agentplane/app"]),
        ("runtime_proxy_session_callers", ["spiffe://agentplane/sessions"]),
        ("runtime_proxy_controller_callers", ["spiffe://agentplane/sandbox-controller"]),
        ("executor_service_callers", ["spiffe://agentplane/central-sampler"]),
    ],
)
def test_dispatcher_rejects_retired_runtime_settings(key: str, value: object) -> None:
    source = _settings().model_dump(mode="json")
    source[key] = value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)


@pytest.mark.parametrize("qps", [-1, 0, 25.5])
def test_gateway_accepts_kubernetes_client_qps_modes(qps: float) -> None:
    assert _settings(kubernetes_client_qps=qps).kubernetes_client_qps == qps


@pytest.mark.parametrize("qps", [-2, -0.5, float("nan"), float("inf")])
def test_gateway_rejects_invalid_kubernetes_client_qps(qps: float) -> None:
    with pytest.raises(ValidationError):
        _settings(kubernetes_client_qps=qps)


def test_shared_listener_svid_requires_tls() -> None:
    source = _settings().model_dump(mode="json")
    source["rpc"]["tls"] = None
    with pytest.raises(ValidationError, match="spiffe requires tls"):
        Settings.model_validate(source)


def test_control_service_tls_rejects_unrelated_identity() -> None:
    source = _settings().model_dump(mode="json")
    source["gateway_tls"]["server_uri"] = "spiffe://agentplane/runner-gateway"
    with pytest.raises(
        ValidationError, match=r"gateway_tls\.server_uri must identify the Gateway or Environments Runtime proxy"
    ):
        Settings.model_validate(source)


@pytest.mark.parametrize(
    "server_uri", ["spiffe://agentplane/session-dispatcher", "spiffe://agentplane/sandbox-controller"]
)
def test_runtime_proxy_peer_accepts_each_cutover_receiver(server_uri: str) -> None:
    source = _settings().model_dump(mode="json")
    source["runner_token_file"] = None
    source["gateway_tls"].update(
        server_uri=server_uri,
        cert_file="/test/sessions-cert",
        key_file="/test/sessions-key",
        client_uri="spiffe://agentplane/sessions",
    )
    configured = Settings.model_validate(source)
    assert configured.gateway_tls.server_uri == server_uri
    assert configured.runner_token_file is None


def test_gateway_sessions_svid_is_an_optional_tokenless_caller_mode() -> None:
    source = _settings().model_dump(mode="json")
    source["runner_token_file"] = None
    source["gateway_tls"].update(
        cert_file="/test/sessions-cert", key_file="/test/sessions-key", client_uri="spiffe://agentplane/sessions"
    )
    settings = Settings.model_validate(source)
    assert settings.runner_token_file is None
    assert settings.gateway_tls.client_uri == "spiffe://agentplane/sessions"

    source["runner_token_file"] = "/run/token"
    with pytest.raises(ValidationError, match="service SVID and bearer token authentication cannot be combined"):
        Settings.model_validate(source)

    source["runner_token_file"] = None
    source["gateway_tls"]["client_uri"] = "spiffe://agentplane/app"
    with pytest.raises(ValidationError, match="client_uri must be 'spiffe://agentplane/sessions'"):
        Settings.model_validate(source)

    source["gateway_tls"]["client_uri"] = None
    source["gateway_tls"].pop("cert_file")
    source["gateway_tls"].pop("key_file")
    with pytest.raises(ValidationError, match="a bearer token file or service SVID is required"):
        Settings.model_validate(source)


def test_central_sampler_sessions_svid_is_an_optional_tokenless_caller_mode() -> None:
    source = _settings().model_dump(mode="json")
    source["central_sampler"] = {
        "target": "sampler:8080",
        "token_file": None,
        "tls": {
            "ca_file": "/test/workload-ca",
            "server_uri": "spiffe://agentplane/central-sampler",
            "cert_file": "/test/sessions-cert",
            "key_file": "/test/sessions-key",
            "client_uri": "spiffe://agentplane/sessions",
        },
    }
    settings = Settings.model_validate(source)
    assert settings.central_sampler is not None
    assert settings.central_sampler.token_file is None
    assert settings.central_sampler.tls is not None
    assert settings.central_sampler.tls.client_uri == "spiffe://agentplane/sessions"

    source["central_sampler"]["token_file"] = "/run/token"
    with pytest.raises(ValidationError, match="service SVID and bearer token authentication cannot be combined"):
        Settings.model_validate(source)

    source["central_sampler"]["token_file"] = None
    source["central_sampler"]["tls"]["client_uri"] = "spiffe://agentplane/app"
    with pytest.raises(ValidationError, match="client_uri must be 'spiffe://agentplane/sessions'"):
        Settings.model_validate(source)

    source["central_sampler"]["tls"]["client_uri"] = "spiffe://agentplane/sessions"
    source["central_sampler"]["tls"]["server_uri"] = "spiffe://agentplane/policy-controller"
    with pytest.raises(ValidationError, match=r"central_sampler\.tls\.server_uri must identify the Central Sampler"):
        Settings.model_validate(source)

    source["central_sampler"]["tls"]["server_uri"] = "spiffe://agentplane/central-sampler"
    source["central_sampler"]["tls"]["client_uri"] = None
    source["central_sampler"]["tls"].pop("cert_file")
    source["central_sampler"]["tls"].pop("key_file")
    with pytest.raises(ValidationError, match="a bearer token file or service SVID is required"):
        Settings.model_validate(source)


def test_service_settings_require_spiffe_listener_authentication() -> None:
    source = _settings().model_dump(mode="json")
    source["rpc"]["auth"] = [{"mode": "kubernetes_bearer", "audiences": ["unused"]}]
    source["dispatcher_service_callers"] = ["spiffe://agentplane/actions"]
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        Settings.model_validate(source)


def test_empty_service_settings_are_omitted_from_deployment_output() -> None:
    source = _settings().model_dump(mode="json")
    new_optional_fields = {"dispatcher_service_delegation", "dispatcher_service_callers", "wake_service_callers"}
    assert new_optional_fields.isdisjoint(source)


def test_dispatcher_service_delegation_is_scoped_to_dispatcher_and_action_provenance() -> None:
    source = _settings().model_dump(mode="json")
    source["dispatcher_service_delegation"] = {
        "actors": [{"spiffe_id": "spiffe://agentplane/actions"}],
        "methods": ["/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput"],
    }
    assert Settings.model_validate(source).dispatcher_service_delegation is not None

    source["dispatcher_service_delegation"]["methods"] = [
        "/agentplane.environments.v1.EnvironmentService/ListEnvironments"
    ]
    with pytest.raises(ValidationError, match="dispatcher_service_delegation methods must belong to SessionDispatcher"):
        Settings.model_validate(source)

    source["dispatcher_service_delegation"]["methods"] = [
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput"
    ]
    source["dispatcher_service_delegation"]["actors"][0]["connection_provenance"] = True
    with pytest.raises(ValidationError):
        Settings.model_validate(source)

    source["dispatcher_service_delegation"]["actors"] = [
        {"spiffe_id": "spiffe://agentplane/test/sandbox/credential-id"}
    ]
    with pytest.raises(ValidationError):
        Settings.model_validate(source)


def test_static_settings_reject_runtime_inputs_and_legacy_namespace_authority() -> None:
    source = _settings().model_dump(mode="json")
    source["dispatcher_database_url"] = "postgresql://secret.invalid/dispatcher"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)

    source = _settings().model_dump(mode="json")
    source["pod_ip"] = "192.0.2.10"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)

    source = _settings().model_dump(mode="json")
    source["delegation"] = {
        "actor": "test-app/actions",
        "identity_callers": False,
        "methods": ["/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput"],
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(source)

    source["delegation"]["identity_callers"] = True
    source["delegation"]["caller_namespaces"] = ["test-app"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)


def test_runtime_inputs_are_not_settings_fields() -> None:
    assert "dispatcher_database_url" not in Settings.model_fields
    assert Settings.config_file_env == "AGENTPLANE_SESSIONS_CONFIG_FILE"
    assert Settings.dispatcher_database_url_env == "AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL"


def test_settings_ignore_static_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_SESSIONS_SANDBOX_NAMESPACE", "environment-value")
    monkeypatch.setenv("AGENTPLANE_SESSIONS_RPC__LISTEN_ADDRESS", "127.0.0.1:9999")
    settings = _settings()
    assert settings.sandbox_namespace == "test-sandboxes"
    assert settings.rpc.listen_address == ":8080"


if __name__ == "__main__":
    pytest_bazel.main()
