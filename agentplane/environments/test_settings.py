"""The opt-in receiver settings preserve the existing deployment contract."""

import copy

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.environments.settings import EnvironmentService, Settings


def receiver_settings() -> dict[str, object]:
    return {
        "rpc": {
            "listen_address": ":8082",
            "tls": {"cert_file": "/run/test/tls.crt", "key_file": "/run/test/tls.key"},
            "auth": [{"mode": "spiffe", "ca_file": "/run/test/ca.crt"}],
        },
        "policy_namespace": "test-policy",
        "audience": "test-dispatcher",
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


def test_receiver_roles_and_typed_delegation() -> None:
    settings = EnvironmentService.model_validate(receiver_settings())
    assert settings.service_operators == ("spiffe://agentplane/app",)
    assert settings.service_delegation is not None
    assert settings.service_delegation.actors[0].spiffe_id == "spiffe://agentplane/actions"


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
        {"delegation": {"actor": "test/actions", "identity_callers": True, "methods": ["/other.Service/Method"]}},
        {"unknown": True},
    ],
)
def test_receiver_rejects_unreviewed_authentication(updates: dict[str, object]) -> None:
    settings = receiver_settings() | updates
    with pytest.raises(ValidationError):
        EnvironmentService.model_validate(settings)


def test_receiver_bearer_audience_must_match() -> None:
    settings = EnvironmentService.model_validate(receiver_settings())
    source = copy.deepcopy(settings.model_dump(mode="json"))
    source["rpc"]["auth"].append({"mode": "kubernetes_bearer", "audiences": ["wrong-audience"]})
    with pytest.raises(ValidationError, match="bearer audience"):
        EnvironmentService.model_validate(source)


if __name__ == "__main__":
    pytest_bazel.main()
