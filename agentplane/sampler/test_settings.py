from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentplane.sampler.settings import MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS, ConnectionConfig, Settings


def service_tls(server: str) -> dict[str, str]:
    return {
        "ca_file": "/fixture/workload-ca.pem",
        "server_uri": f"spiffe://agentplane/{server}",
        "cert_file": "/fixture/tls.crt",
        "key_file": "/fixture/tls.key",
        "client_uri": "spiffe://agentplane/central-sampler",
    }


def settings_data() -> dict[str, Any]:
    return {
        "environment_namespace": "test",
        "listen_tls": {
            "listen": "127.0.0.1:9000",
            "cert_file": Path("/fixture/tls.crt"),
            "key_file": Path("/fixture/tls.key"),
        },
        "connections": {
            "provider": {
                "base_url": "https://provider.invalid",
                "auth": {"type": "bearer", "credential": "fixture-key"},
            }
        },
        "models": {
            "fixture-model": {
                "connection": "provider",
                "provider_model": "fixture-model",
                "api": "responses",
                "responses": {"dialect": "lite"},
                "exec_encoding": "freeform",
                "reasoning_efforts": {"high"},
            }
        },
        "code_mode_host": Path("/fixture/code-mode-host"),
        "executor_codex": "codex",
        "logs": Path("/fixture/logs"),
        "max_live_sessions": 1,
        "rpc_timeout_seconds": 30,
        "lease_seconds": 60,
        "gateway_target": "gateway.invalid",
        "gateway_token_file": Path("/fixture/gateway-token"),
        "gateway_tls": {"ca_file": "/fixture/workload-ca.pem", "server_uri": "spiffe://agentplane/session-dispatcher"},
    }


def test_actions_are_optional_and_only_accept_service_mtls() -> None:
    assert Settings(**settings_data()).actions is None
    configured = Settings.model_validate(
        {
            **settings_data(),
            "actions": {"target": "actions:8080", "tls": {"workload_api_socket": "unix:///run/spire/api.sock"}},
        }
    )
    assert configured.actions is not None
    assert configured.actions.tls.workload_api_socket == "unix:///run/spire/api.sock"
    for actions in (
        {"target": "actions:8080"},
        {"target": " ", "tls": {"workload_api_socket": "unix:///run/spire/api.sock"}},
        {"target": "actions:8080", "tls": {}, "token_file": "/run/token"},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**settings_data(), "actions": actions})


def test_admin_service_callers_are_separate_and_omitted_when_empty() -> None:
    settings = Settings.model_validate(settings_data())
    assert settings.admin_service_callers == []
    assert "admin_service_callers" not in settings.model_dump(mode="json", exclude_none=True)

    configured = Settings.model_validate({**settings_data(), "admin_service_callers": ["spiffe://agentplane/app"]})
    assert configured.admin_service_callers == ["spiffe://agentplane/app"]
    assert configured.model_dump(mode="json", exclude_none=True)["admin_service_callers"] == ["spiffe://agentplane/app"]
    with pytest.raises(ValidationError):
        Settings.model_validate({**settings_data(), "admin_service_callers": ["not-a-service"]})


def test_tls_application_listener_is_required_and_plaintext_listener_is_unknown() -> None:
    data = settings_data()
    settings = Settings.model_validate(data)
    assert settings.listen_tls.listen == "127.0.0.1:9000"
    assert settings.health_listen is None
    serialized = settings.model_dump(mode="json", exclude_none=True)
    assert "listen" not in serialized
    assert "health_listen" not in serialized

    configured = Settings.model_validate({**data, "health_listen": "127.0.0.1:8081"})
    assert configured.health_listen == "127.0.0.1:8081"
    with pytest.raises(ValidationError):
        Settings.model_validate({**data, "unknown_listener": "0.0.0.0:8081"})
    with pytest.raises(ValidationError):
        Settings.model_validate({**data, "listen": "127.0.0.1:8080"})
    without_tls = dict(data)
    del without_tls["listen_tls"]
    with pytest.raises(ValidationError):
        Settings.model_validate(without_tls)


def test_provider_phase_budgets_are_optional_and_independent() -> None:
    settings = Settings(**settings_data())
    assert settings.gateway_tls is not None
    assert settings.gateway_tls.server_uri == "spiffe://agentplane/session-dispatcher"
    assert settings.provider_response_header_timeout_seconds is None
    assert settings.provider_idle_timeout_seconds is None

    configured = Settings.model_validate(
        {**settings_data(), "provider_response_header_timeout_seconds": 120, "provider_idle_timeout_seconds": 600}
    )
    assert configured.provider_response_header_timeout_seconds == 120
    assert configured.provider_idle_timeout_seconds == 600


def test_model_response_header_timeout_validation() -> None:
    data = settings_data()
    data["models"]["fixture-model"]["response_header_timeout_seconds"] = 360
    settings = Settings.model_validate(data)
    assert settings.models["fixture-model"].response_header_timeout_seconds == 360
    data["models"]["fixture-model"]["response_header_timeout_seconds"] = MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


@pytest.mark.parametrize("server", ["session-dispatcher", "sandbox-controller"])
def test_runtime_proxy_peer_accepts_each_cutover_receiver(server: str) -> None:
    configured = Settings.model_validate(
        {**settings_data(), "gateway_tls": service_tls(server), "gateway_token_file": None}
    )
    assert configured.gateway_tls is not None
    assert configured.gateway_tls.server_uri == f"spiffe://agentplane/{server}"


def test_gateway_tls_must_name_runtime_proxy_listener() -> None:
    with pytest.raises(ValidationError, match=r"gateway_tls\.server_uri"):
        Settings.model_validate(
            {
                **settings_data(),
                "gateway_tls": {
                    "ca_file": "/fixture/workload-ca.pem",
                    "server_uri": "spiffe://agentplane/runner-gateway",
                },
            }
        )


@pytest.mark.parametrize(("service", "server"), [("history", "history-ingress"), ("gateway", "session-dispatcher")])
def test_service_targets_accept_svid_without_a_token(service: str, server: str) -> None:
    data = settings_data()
    data[f"{service}_target"] = "service.invalid:8080"
    data.pop(f"{service}_token_file", None)
    data[f"{service}_tls"] = service_tls(server)
    configured = Settings.model_validate(data).model_dump(mode="json", exclude_none=True)
    assert f"{service}_token_file" not in configured
    assert configured[f"{service}_tls"]["client_uri"] == "spiffe://agentplane/central-sampler"

    with pytest.raises(ValidationError, match="cannot be combined"):
        Settings.model_validate({**data, f"{service}_token_file": "/fixture/token"})
    with pytest.raises(ValidationError, match="client_uri must be"):
        Settings.model_validate(
            {**data, f"{service}_tls": {**service_tls(server), "client_uri": "spiffe://agentplane/app"}}
        )


def test_ingress_can_use_service_svid_or_rotating_bearer() -> None:
    connection = {
        "base_url": "https://llm-ingress.invalid",
        "auth": {"type": "agentplane_ingress"},
        "tls": service_tls("llm-ingress"),
    }
    configured = ConnectionConfig.model_validate(connection).model_dump(mode="json", exclude_none=True)
    assert configured["auth"] == {"type": "agentplane_ingress"}
    with pytest.raises(ValidationError, match="cannot be combined"):
        ConnectionConfig.model_validate(
            {**connection, "auth": {"type": "agentplane_ingress", "credential_file": "/fixture/token"}}
        )
    bearer = ConnectionConfig.model_validate(
        {
            **connection,
            "auth": {"type": "agentplane_ingress", "credential_file": "/fixture/token"},
            "tls": {"ca_file": "/fixture/workload-ca.pem", "server_uri": "spiffe://agentplane/llm-ingress"},
        }
    )
    assert bearer.auth.credential_file == Path("/fixture/token")


@pytest.mark.parametrize(
    "changes",
    [
        {"tls": None},
        {"tls": service_tls("history-ingress")},
        {"tls": {**service_tls("llm-ingress"), "client_uri": "spiffe://agentplane/app"}},
        {"tls": {**service_tls("llm-ingress"), "key_file": None}},
        {"auth": {"type": "bearer", "credential": "provider-key"}},
        {"auth": {"type": "anthropic_api_key", "credential": "provider-key"}},
    ],
)
def test_ingress_rejects_missing_wrong_or_provider_service_credentials(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate(
            {
                "base_url": "https://llm-ingress.invalid",
                "auth": {"type": "agentplane_ingress"},
                "tls": service_tls("llm-ingress"),
                **changes,
            }
        )


def test_turn_timeout_is_optional_and_configurable() -> None:
    settings = Settings(**settings_data())
    assert settings.turn_timeout_seconds is None
    assert "turn_timeout_seconds" not in settings.model_dump(mode="json", exclude_none=True)

    configured = Settings.model_validate({**settings_data(), "turn_timeout_seconds": 120})
    assert configured.turn_timeout_seconds == 120


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()


def test_environment_namespace_is_required() -> None:
    data = settings_data()
    del data["environment_namespace"]
    with pytest.raises(ValidationError):
        Settings(**data)
    for namespace in ("test", "test-ns", "a"):
        assert Settings(**{**settings_data(), "environment_namespace": namespace}).environment_namespace == namespace
    for namespace in ("", "bad.namespace", "Upper", "-bad", "a" * 64):
        with pytest.raises(ValidationError):
            Settings(**{**settings_data(), "environment_namespace": namespace})
