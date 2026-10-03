"""KubeVirt seed validation and translation into runner-owned launch settings."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.runner.guest_config import GuestConfig, make_runner_config


def _config(**updates: object) -> GuestConfig:
    values: dict[str, object] = {
        "version": 1,
        "environment_id": "vm-uid-123",
        "listen": "0.0.0.0:7000",
        "llm_base_url": "http://agentplane-llm-ingress.agentplane.svc.cluster.local:8080",
        "proxy_url": "http://10.0.2.2:3128",
        "model_context_windows": {"test-model": 128000},
        "format_blank_disks": ["state", "workspace"],
    }
    return GuestConfig.model_validate(values | updates)


def test_guest_config_builds_explicit_proxy_and_persistent_paths() -> None:
    runner = make_runner_config(_config())

    assert runner.state_dir.as_posix() == "/state"
    assert runner.native_state_dir is not None
    assert runner.native_state_dir.as_posix() == "/state/native"
    assert runner.initialization_cwd is not None
    assert runner.initialization_cwd.as_posix() == "/workspace"
    assert runner.workspace_root is not None
    assert runner.workspace_root.as_posix() == "/workspace"
    assert runner.claude is not None
    assert runner.claude.base_url == "http://agentplane-llm-ingress.agentplane.svc.cluster.local:8080"
    assert runner.codex is not None
    assert runner.codex.base_url.endswith(":8080/v1")
    assert runner.claude.auth_token == "agentplane-credential-agentplane-workload"
    assert runner.codex.api_key == runner.claude.auth_token
    assert runner.environment["HTTP_PROXY"] == "http://10.0.2.2:3128"
    assert runner.environment["HTTPS_PROXY"] == runner.environment["HTTP_PROXY"]
    assert runner.environment["KUBECONFIG"] == "/run/agentplane/kubeconfig"
    assert runner.environment["SSL_CERT_FILE"] == "/run/agentplane/ca-certificates.crt"
    assert runner.model_context_windows == {"test-model": 128000}


@pytest.mark.parametrize(
    "updates",
    [
        {"version": 2},
        {"listen": "127.0.0.1:7000"},
        {"llm_base_url": "https://user:password@model.test"},
        {"llm_base_url": "https://model.test/v1"},
        {"proxy_url": "http://proxy.test/path"},
        {"model_context_windows": {"test-model": True}},
        {"model_context_windows": {"test-model": 0}},
        {"format_blank_disks": ["state", "state"]},
        {"unexpected": "value"},
    ],
)
def test_invalid_guest_settings_fail_before_runner_start(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _config(**updates)


if __name__ == "__main__":
    pytest_bazel.main()
