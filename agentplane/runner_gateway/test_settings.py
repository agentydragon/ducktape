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
                "dispatcher_database_url": "postgresql://test:test@db.invalid/dispatcher",
                "pod_ip": "192.0.2.10",
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


if __name__ == "__main__":
    pytest_bazel.main()
