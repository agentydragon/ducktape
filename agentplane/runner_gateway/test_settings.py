"""Runner Gateway rejects ambiguous control security in its authored deployment settings."""

from typing import cast

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.runner_gateway.settings import Settings


def _settings(runner_security: object, *, kubernetes_client_qps: float = 0) -> Settings:
    return cast(
        Settings,
        Settings.model_validate(
            {
                "sandbox_namespace": "test-sandboxes",
                "policy_namespace": "test-policies",
                "runner_token_file": "/run/test/token",
                "dispatcher_audience": "test-dispatcher",
                "gateway_audience": "test-gateway",
                "gateway_tls": {"ca_file": "/test/workload-ca", "server_uri": "spiffe://agentplane/session-dispatcher"},
                "rpc": {
                    "listen_address": ":8080",
                    "tls": {"cert_file": "/test/workload-cert", "key_file": "/test/workload-key"},
                    "auth": [
                        {"mode": "spiffe", "ca_file": "/test/workload-ca"},
                        {"mode": "kubernetes_bearer", "audiences": ["test-dispatcher", "test-gateway"]},
                    ],
                },
                "dispatcher_callers": {"test-app/test-dispatcher"},
                "gateway_callers": {"test-app/test-gateway"},
                "operator_callers": {"test-app/test-dispatcher"},
                "runner_security": runner_security,
                "kubernetes_client_qps": kubernetes_client_qps,
            }
        ),
    )


@pytest.mark.parametrize(
    "security",
    [
        {"mode": "mtls", "tls": {"cert_file": "/test/cert", "key_file": "/test/key"}},
        {"mode": "insecure", "tls": {"cert_file": "/test/cert", "key_file": "/test/key", "ca_file": "/test/ca"}},
        {"mode": "unknown"},
        {"tls": {"cert_file": "/test/cert", "key_file": "/test/key", "ca_file": "/test/ca"}},
    ],
    ids=["incomplete-mtls", "insecure-with-tls", "unknown-mode", "missing-mode"],
)
def test_gateway_rejects_ambiguous_runner_security(security: object) -> None:
    with pytest.raises(ValidationError):
        _settings(security)


def test_gateway_requires_explicit_runner_security_mode() -> None:
    with pytest.raises(ValidationError):
        _settings(None)


@pytest.mark.parametrize("qps", [-1, 0, 25.5])
def test_gateway_accepts_kubernetes_client_qps_modes(qps: float) -> None:
    assert _settings({"mode": "insecure"}, kubernetes_client_qps=qps).kubernetes_client_qps == qps


@pytest.mark.parametrize("qps", [-2, -0.5, float("nan"), float("inf")])
def test_gateway_rejects_invalid_kubernetes_client_qps(qps: float) -> None:
    with pytest.raises(ValidationError):
        _settings({"mode": "insecure"}, kubernetes_client_qps=qps)


def test_operator_callers_are_an_optional_subset_of_dispatcher_callers() -> None:
    settings = _settings({"mode": "insecure"})
    assert settings.operator_callers == {"test-app/test-dispatcher"}
    source = settings.model_dump()
    source["operator_callers"] = {"test-app/other"}
    with pytest.raises(ValidationError, match="operator_callers must be a subset"):
        Settings.model_validate(source)


def test_shared_listener_bearer_audiences_are_exactly_method_scoped() -> None:
    source = _settings({"mode": "insecure"}).model_dump(mode="json")
    source["rpc"]["auth"][1]["audiences"] = ["test-dispatcher"]
    with pytest.raises(ValidationError, match="exactly dispatcher_audience and gateway_audience"):
        Settings.model_validate(source)


def test_shared_listener_svid_requires_tls() -> None:
    source = _settings({"mode": "insecure"}).model_dump(mode="json")
    source["rpc"]["tls"] = None
    with pytest.raises(ValidationError, match="spiffe requires tls"):
        Settings.model_validate(source)


def test_control_service_tls_pins_session_dispatcher_identity() -> None:
    source = _settings({"mode": "insecure"}).model_dump(mode="json")
    source["gateway_tls"]["server_uri"] = "spiffe://agentplane/runner-gateway"
    with pytest.raises(ValidationError, match=r"gateway_tls\.server_uri must identify the Session Dispatcher service"):
        Settings.model_validate(source)


def test_static_settings_reject_runtime_inputs_and_legacy_namespace_authority() -> None:
    source = _settings({"mode": "insecure"}).model_dump(mode="json")
    source["dispatcher_database_url"] = "postgresql://secret.invalid/dispatcher"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)

    source = _settings({"mode": "insecure"}).model_dump(mode="json")
    source["pod_ip"] = "192.0.2.10"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)

    source = _settings({"mode": "insecure"}).model_dump(mode="json")
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
    assert "pod_name" not in Settings.model_fields
    assert "pod_ip" not in Settings.model_fields
    assert Settings.config_file_env == "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE"
    assert Settings.dispatcher_database_url_env == "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    assert Settings.pod_name_env == "AGENTPLANE_RUNNER_GATEWAY_POD_NAME"
    assert Settings.pod_ip_env == "AGENTPLANE_RUNNER_GATEWAY_POD_IP"


def test_account_sets_serialize_in_sorted_order() -> None:
    source = _settings({"mode": "insecure"}).model_dump(mode="python")
    source["dispatcher_callers"] = {"test-a-z/app", "test-a/worker"}
    source["operator_callers"] = {"test-a/worker"}
    source["environment_readers"] = {"test-a-z/app", "test-a/worker"}
    settings = Settings.model_validate(source)
    rendered = settings.model_dump(mode="json")
    assert rendered["dispatcher_callers"] == ["test-a/worker", "test-a-z/app"]
    assert rendered["environment_readers"] == ["test-a/worker", "test-a-z/app"]


def test_settings_ignore_static_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_AUDIENCE", "environment-value")
    monkeypatch.setenv("AGENTPLANE_RUNNER_GATEWAY_RPC__LISTEN_ADDRESS", "127.0.0.1:9999")
    settings = _settings({"mode": "insecure"})
    assert settings.dispatcher_audience == "test-dispatcher"
    assert settings.rpc.listen_address == ":8080"


if __name__ == "__main__":
    pytest_bazel.main()
