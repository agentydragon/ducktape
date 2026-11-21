"""The opt-in receiver settings preserve the existing deployment contract."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.environments.settings import EnvironmentService, RuntimeProxy, Settings


def receiver_settings() -> dict[str, object]:
    return {
        "rpc": {
            "listen_address": ":8082",
            "tls": {"cert_file": "/run/test/tls.crt", "key_file": "/run/test/tls.key"},
            "auth": [{"mode": "spiffe", "ca_file": "/run/test/ca.crt"}],
        },
        "policy_namespace": "test-policy",
        "service_operators": ["spiffe://agentplane/app"],
        "service_delegation": {
            "actors": [{"spiffe_id": "spiffe://agentplane/actions"}],
            "methods": ["/agentplane.environments.v1.EnvironmentService/ResumeEnvironment"],
        },
    }


def test_receiver_omission_does_not_add_a_deployment_key() -> None:
    settings = Settings.model_validate(
        {
            "namespace": "test-environments",
            "runner_security": {"mode": "insecure"},
            "runner_gateway_target": "runner-gateway.test.svc:8080",
            "runner_gateway_token_file": "/run/test/token",
        }
    )
    assert settings.environment_service is None
    assert "environment_service" not in settings.model_dump(mode="json")


def test_runtime_proxy_omission_does_not_add_a_deployment_key() -> None:
    receiver = EnvironmentService.model_validate(receiver_settings())
    assert receiver.runtime_proxy is None
    assert "runtime_proxy" not in receiver.model_dump(mode="json")


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"runner_security": {"mode": "unsupported"}},
        {"runner_security": {"mode": "insecure"}, "audience": "obsolete"},
        {"runner_security": {"mode": "insecure"}, "unknown": True},
    ],
)
def test_runtime_proxy_rejects_incomplete_or_unknown_settings(settings: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RuntimeProxy.model_validate(settings)


@pytest.mark.parametrize("field", ["readers", "session_callers", "controller_callers", "executor_callers"])
def test_runtime_proxy_rejects_unknown_service_callers(field: str) -> None:
    RuntimeProxy.model_validate({"runner_security": {"mode": "insecure"}, field: ["spiffe://agentplane/app"]})
    with pytest.raises(ValidationError):
        RuntimeProxy.model_validate({"runner_security": {"mode": "insecure"}, field: ["spiffe://agentplane/unknown"]})


@pytest.mark.parametrize(
    "updates",
    [
        {"rpc": {"listen_address": ":8082", "auth": [{"mode": "kubernetes_bearer", "audiences": ["test-dispatcher"]}]}},
        {"service_operators": ["spiffe://agentplane/unknown"]},
        {
            "service_delegation": {
                "actors": [{"spiffe_id": "spiffe://agentplane/actions", "connection_provenance": True}],
                "methods": ["/agentplane.environments.v1.EnvironmentService/ResumeEnvironment"],
            }
        },
        {
            "service_delegation": {
                "actors": [{"spiffe_id": "spiffe://agentplane/actions"}],
                "methods": ["/other.Service/ResumeEnvironment"],
            }
        },
        {"unknown": True},
    ],
)
def test_receiver_rejects_unreviewed_authentication(updates: dict[str, object]) -> None:
    settings = receiver_settings() | updates
    with pytest.raises(ValidationError):
        EnvironmentService.model_validate(settings)


if __name__ == "__main__":
    pytest_bazel.main()
