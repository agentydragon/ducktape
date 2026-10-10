"""KubeVirt seed validation and translation into runner-owned launch settings."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.runner import guest_config
from agentplane.runner.guest_config import GuestConfig, make_runner_config


def _config(**updates: object) -> GuestConfig:
    values: dict[str, object] = {
        "version": 1,
        "environment_id": "vm-uid-123",
        "listen": "0.0.0.0:7000",
        "llm_base_url": "http://agentplane-llm-ingress.agentplane.svc.cluster.local:8080",
        "proxy_url": "http://10.0.2.2:3128",
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
    assert runner.harness_environment["HTTP_PROXY"] == "http://10.0.2.2:3128"
    assert runner.harness_environment["HTTPS_PROXY"] == runner.harness_environment["HTTP_PROXY"]
    assert runner.harness_environment["KUBECONFIG"] == "/run/agentplane/kubeconfig"
    assert runner.harness_environment["SSL_CERT_FILE"] == "/run/agentplane/ca-certificates.crt"


@pytest.mark.parametrize(
    "updates",
    [
        {"version": 2},
        {"listen": "127.0.0.1:7000"},
        {"llm_base_url": "https://user:password@model.test"},
        {"llm_base_url": "https://model.test/v1"},
        {"proxy_url": "http://proxy.test/path"},
        {"format_blank_disks": ["state", "state"]},
        {"unexpected": "value"},
    ],
)
def test_invalid_guest_settings_fail_before_runner_start(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _config(**updates)


def test_guest_proxy_and_ca_are_installed_in_the_runner_process(monkeypatch: pytest.MonkeyPatch) -> None:
    runner_environment: dict[str, str] = {}
    monkeypatch.setattr(guest_config.os, "environ", runner_environment)

    guest_config.configure_runner_network_environment(make_runner_config(_config()).harness_environment)

    assert runner_environment["HTTP_PROXY"] == "http://10.0.2.2:3128"
    assert runner_environment["HTTPS_PROXY"] == "http://10.0.2.2:3128"
    assert runner_environment["SSL_CERT_FILE"] == "/run/agentplane/ca-certificates.crt"


if __name__ == "__main__":
    pytest_bazel.main()
