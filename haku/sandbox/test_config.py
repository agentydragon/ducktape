"""Configuration tests for the single Agent Sandbox environment."""

from copy import deepcopy
from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.sandbox.config import SandboxEnvironmentConfig

RAW_CONFIG: dict[str, Any] = {
    "sandbox": {
        "namespace": "agent-workspaces",
        "warm_pool": "haku",
        "container": "workspace",
        "default_cwd": "/workspace/haku-state",
        "initial_ttl_seconds": 28_800,
        "exec_ttl_extension_seconds": 7_200,
        "provisioning_timeout_seconds": 600,
        "max_exec_timeout_seconds": 300,
        "max_output_bytes": 100_000,
    },
    "bootstrap": {
        "cwd": "/workspace",
        "timeout_seconds": 300,
        "script": "git clone http://forgejo/haku/haku-state.git",
    },
}


def test_environment_uses_seconds_and_has_stable_contract_hash() -> None:
    first = SandboxEnvironmentConfig.model_validate(RAW_CONFIG)
    second = SandboxEnvironmentConfig.model_validate(RAW_CONFIG)

    assert first.sandbox.initial_ttl_seconds == 28_800
    assert first.contract_hash == second.contract_hash
    assert len(first.contract_hash) == 64


def test_contract_hash_changes_with_bootstrap() -> None:
    first = SandboxEnvironmentConfig.model_validate(RAW_CONFIG)
    changed = deepcopy(RAW_CONFIG)
    changed["bootstrap"]["script"] = "echo changed"

    assert first.contract_hash != SandboxEnvironmentConfig.model_validate(changed).contract_hash


def test_initial_ttl_must_cover_provisioning_and_bootstrap() -> None:
    raw = deepcopy(RAW_CONFIG)
    raw["sandbox"]["initial_ttl_seconds"] = 900

    with pytest.raises(ValidationError, match="must exceed provisioning_timeout_seconds"):
        SandboxEnvironmentConfig.model_validate(raw)


def test_exec_extension_must_cover_longest_exec() -> None:
    raw = deepcopy(RAW_CONFIG)
    raw["sandbox"]["exec_ttl_extension_seconds"] = 299

    with pytest.raises(ValidationError, match="must be at least"):
        SandboxEnvironmentConfig.model_validate(raw)


if __name__ == "__main__":
    pytest_bazel.main()
