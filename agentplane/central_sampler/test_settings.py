from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentplane.central_sampler.settings import MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS, Settings


def settings_data() -> dict[str, Any]:
    return {
        "listen": "127.0.0.1:9000",
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
        "audience": "fixture-sampler",
        "allowed_accounts": {"fixture/client"},
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


def test_gateway_tls_must_name_runner_gateway_rpc_listener() -> None:
    # The serving Gateway listener uses the Session Dispatcher service identity; GatewayURI is
    # reserved for the Gateway's client identity on runner-control mTLS.
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


@pytest.mark.parametrize("field", ["provider_response_header_timeout_seconds", "provider_idle_timeout_seconds"])
@pytest.mark.parametrize("seconds", [0, -1, MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS + 1])
def test_provider_phase_budgets_must_be_positive(field: str, seconds: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**settings_data(), field: seconds})


def test_turn_timeout_is_optional_and_configurable() -> None:
    settings = Settings(**settings_data())
    assert settings.turn_timeout_seconds is None
    assert "turn_timeout_seconds" not in settings.model_dump(mode="json", exclude_none=True)

    configured = Settings.model_validate({**settings_data(), "turn_timeout_seconds": 120})
    assert configured.turn_timeout_seconds == 120


@pytest.mark.parametrize("seconds", [0, -1, MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS + 1])
def test_turn_timeout_must_be_positive_and_fit_in_duration(seconds: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**settings_data(), "turn_timeout_seconds": seconds})


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
