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


def _digest(raw: dict[str, Any]) -> str:
    return SandboxEnvironmentConfig.model_validate(raw).bootstrap.script_digest


def test_script_digest_identifies_the_script_and_nothing_else() -> None:
    rescripted = deepcopy(RAW_CONFIG)
    rescripted["bootstrap"]["script"] = "echo changed"
    # A per-call ceiling cannot change what a running box was bootstrapped with, so it must not
    # move the digest a live claim is judged against.
    retuned = deepcopy(RAW_CONFIG)
    retuned["sandbox"]["max_output_bytes"] = 50_000

    assert _digest(RAW_CONFIG) == _digest(deepcopy(RAW_CONFIG))
    assert _digest(RAW_CONFIG) != _digest(rescripted)
    assert _digest(RAW_CONFIG) == _digest(retuned)


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
