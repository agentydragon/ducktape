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
        "turn_timeout_seconds": 3600,
        "rpc_timeout_seconds": 30,
        "lease_seconds": 60,
        "gateway_target": "gateway.invalid",
        "gateway_token_file": Path("/fixture/gateway-token"),
    }


def test_provider_phase_budgets_are_optional_and_independent() -> None:
    settings = Settings(**settings_data())
    assert settings.provider_response_header_timeout_seconds is None
    assert settings.provider_idle_timeout_seconds is None

    configured = Settings.model_validate(
        {**settings_data(), "provider_response_header_timeout_seconds": 120, "provider_idle_timeout_seconds": 600}
    )
    assert configured.provider_response_header_timeout_seconds == 120
    assert configured.provider_idle_timeout_seconds == 600


@pytest.mark.parametrize("field", ["provider_response_header_timeout_seconds", "provider_idle_timeout_seconds"])
@pytest.mark.parametrize("seconds", [0, -1, MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS + 1])
def test_provider_phase_budgets_must_be_positive(field: str, seconds: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**settings_data(), field: seconds})


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
