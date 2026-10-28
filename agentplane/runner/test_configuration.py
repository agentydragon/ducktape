"""The shared file contract rejects partial and contradictory deployment policy."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.runner.configuration import Configuration, GuestResources, VMEnvironment


def _configuration() -> dict[str, object]:
    return {
        "version": 2,
        "environment": {"kind": "pod", "supervision": "subprocess"},
        "state_dir": "/test/state",
        "initialization_cwd": "/test/workspaces",
        "control": {
            "mode": "reverse",
            "target": "gateway.test:7100",
            "tls": {"cert_file": "/run/test/tls.crt", "key_file": "/run/test/tls.key", "ca_file": "/run/test/ca.crt"},
        },
        "ingress": {"target": "history.test:8081", "bearer": "ingress-placeholder"},
        "platform_instructions": "Test guidance.",
        "harness_environment": {"inherit": ["PATH"], "values": {"HOME": "/test/home"}},
        "codex": {"binary": "/test/codex", "base_url": "http://models.test/v1", "token_env": "TEST_PROVIDER_TOKEN"},
    }


def test_runner_configuration_round_trip_preserves_environment_sources() -> None:
    config = Configuration.model_validate(_configuration())
    decoded = Configuration.model_validate_json(config.model_dump_json(exclude_none=True))
    assert decoded == config
    assert decoded.session_reconcile_concurrency is None
    assert decoded.harness_environment.values["HOME"] == "/test/home"
    assert decoded.harness_environment.inherit == ["PATH"]


def test_runner_configuration_accepts_explicit_session_reconcile_concurrency() -> None:
    values = _configuration() | {"session_reconcile_concurrency": 2048}
    config = Configuration.model_validate(values)
    decoded = Configuration.model_validate_json(config.model_dump_json(exclude_none=True))
    assert decoded.session_reconcile_concurrency == 2048


def test_forward_control_mode_is_explicit_and_round_trips() -> None:
    values = _configuration() | {
        "control": {
            "mode": "forward",
            "listen_address": "0.0.0.0:8090",
            "tls": {
                "cert_file": "/run/runner/tls.crt",
                "key_file": "/run/runner/tls.key",
                "ca_file": "/run/runner/ca.crt",
            },
        }
    }
    config = Configuration.model_validate(values)
    decoded = Configuration.model_validate_json(config.model_dump_json(exclude_none=True))
    assert decoded == config
    assert decoded.control.mode == "forward"


@pytest.mark.parametrize(
    "updates",
    [
        {"version": 1},
        {"session_reconcile_concurrency": 0},
        {"session_reconcile_concurrency": -1},
        {"state_dir": "relative"},
        {"workspace_root": ""},
        {"harness_environment": {"inherit": ["NAME=value"]}},
        {"harness_environment": {"values": {"": "value"}}},
        {"codex": {"binary": "/test/codex"}},
        {"control": {"target": "gateway.test:7100", "bearer": "legacy-token"}},
        {"control": {"mode": "reverse", "target": "gateway.test:7100"}},
        {"control": {"mode": "unknown", "target": "gateway.test:7100"}},
        {
            "control": {
                "mode": "forward",
                "listen_address": "0.0.0.0:notaport",
                "tls": {
                    "cert_file": "/run/test/tls.crt",
                    "key_file": "/run/test/tls.key",
                    "ca_file": "/run/test/ca.crt",
                },
            }
        },
        {
            "control": {
                "mode": "reverse",
                "target": "gateway.test:7100",
                "listen_address": "0.0.0.0:8090",
                "tls": {
                    "cert_file": "/run/test/tls.crt",
                    "key_file": "/run/test/tls.key",
                    "ca_file": "/run/test/ca.crt",
                },
            }
        },
        {
            "control": {
                "mode": "forward",
                "target": "gateway.test:7100",
                "listen_address": "0.0.0.0:8090",
                "tls": {
                    "cert_file": "/run/test/tls.crt",
                    "key_file": "/run/test/tls.key",
                    "ca_file": "/run/test/ca.crt",
                },
            }
        },
        {
            "control": {
                "mode": "forward",
                "listen_address": "0.0.0.0:8090",
                "tls": {"cert_file": "relative.crt", "key_file": "/run/test/tls.key", "ca_file": "/run/test/ca.crt"},
            }
        },
        {"environment": {"kind": "unknown"}},
        {"environment": {"kind": "pod"}},
        {"environment": {"kind": "pod", "supervision": "unknown"}},
        {"environment": {"kind": "pod", "supervision": "subprocess", "console_autologin": True}},
        {"environment": {"kind": "pod", "supervision": "subprocess", "executor": {"url": "ws://127.0.0.1:4501"}}},
        {
            "environment": {
                "kind": "pod",
                "supervision": "subprocess",
                "executor": {"url": "ws://0.0.0.0:4501", "socket": "/test/socket"},
            }
        },
        {
            "environment": {
                "kind": "pod",
                "supervision": "subprocess",
                "executor": {"url": "ws://127.0.0.1:0", "socket": "/test/socket"},
            }
        },
        {
            "environment": {
                "kind": "pod",
                "supervision": "subprocess",
                "executor": {"url": "ws://127.0.0.1:4501?", "socket": "/test/socket"},
            }
        },
        {
            "environment": {
                "kind": "pod",
                "supervision": "subprocess",
                "executor": {"url": "ws://127.0.0.1:4501", "socket": "relative"},
            }
        },
        {
            "environment": {
                "kind": "pod",
                "supervision": "subprocess",
                "executor": {"url": "ws://127.0.0.1:4501", "socket": "/test/socket", "root": "/test/cgroup"},
            }
        },
        {"unexpected": "value"},
    ],
)
def test_configuration_rejects_invalid_policy(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Configuration.model_validate(_configuration() | updates)


def _limits(*, memory: int, pids: int) -> dict[str, object]:
    return {
        "cpu_quota_micros": 100_000,
        "memory_max_bytes": memory,
        "pids_max": pids,
        "read_bps": 52_428_800,
        "write_bps": 20_971_520,
        "read_iops": 1000,
        "write_iops": 500,
    }


def _resources() -> dict[str, object]:
    return {
        "guest_memory_bytes": 1_610_612_736,
        "guest_vcpu_count": 1,
        "os_reserve_bytes": 268_435_456,
        "service_memory_high_bytes": 1_207_959_552,
        "service_memory_max_bytes": 1_342_177_280,
        "service_cpu_quota_micros": 100_000,
        "service_pids_max": 1024,
        "aggregate": _limits(memory=1_140_850_688, pids=768),
        "process": _limits(memory=805_306_368, pids=512),
    }


def test_guest_resource_budgets_are_typed_and_fit_guest_headroom() -> None:
    resources = GuestResources.model_validate(_resources())

    assert resources.guest_memory_bytes == 1_610_612_736
    assert resources.service_memory_max_bytes == resources.guest_memory_bytes - resources.os_reserve_bytes
    assert resources.aggregate.memory_max_bytes == 1_140_850_688
    assert resources.process.memory_max_bytes == 805_306_368


@pytest.mark.parametrize(
    ("memory", "cpu", "pids", "valid"),
    [
        (67_108_864, 100_000, 128, True),
        (67_108_865, 100_000, 128, False),
        (67_108_864, 100_001, 128, False),
        (67_108_864, 100_000, 256, False),
    ],
)
def test_executor_budget_shares_outer_service_headroom(memory: int, cpu: int, pids: int, valid: bool) -> None:
    values = {**_resources(), "executor": {**_limits(memory=memory, pids=pids), "cpu_quota_micros": cpu}}
    if valid:
        resources = GuestResources.model_validate(values)
        assert resources.executor is not None
        assert (
            resources.aggregate.memory_max_bytes + resources.executor.memory_max_bytes
            <= resources.service_memory_high_bytes
        )
    else:
        with pytest.raises(ValidationError):
            GuestResources.model_validate(values)


def _vm_environment() -> dict[str, object]:
    return {
        "kind": "vm",
        "resources": _resources(),
        "agent_uid": 107,
        "agent_gid": 107,
        "block_devices": ["/dev/test-workspace"],
    }


def test_vm_configuration_round_trip_preserves_one_resource_policy() -> None:
    value = _configuration() | {"environment": _vm_environment()}
    config = Configuration.model_validate(value)
    decoded = Configuration.model_validate_json(config.model_dump_json(exclude_none=True))
    assert decoded == config
    assert isinstance(decoded.environment, VMEnvironment)
    assert decoded.environment.resources.service_memory_max_bytes <= (
        decoded.environment.resources.guest_memory_bytes - decoded.environment.resources.os_reserve_bytes
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"resources": {**_resources(), "service_memory_max_bytes": 1_610_612_736}},
        {"resources": {**_resources(), "aggregate": _limits(memory=805_306_367, pids=768)}},
        {"executor": {"url": "ws://127.0.0.1:4501"}},
        {"resources": {**_resources(), "executor": _limits(memory=67_108_864, pids=128)}},
        {"agent_uid": 0},
        {"block_devices": ["relative"]},
    ],
)
def test_vm_environment_rejects_inconsistent_policy(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Configuration.model_validate(_configuration() | {"environment": _vm_environment() | updates})


def test_vm_executor_accepts_endpoint_with_budget() -> None:
    value = _vm_environment() | {
        "resources": {**_resources(), "executor": _limits(memory=67_108_864, pids=128)},
        "executor": {"url": "ws://127.0.0.1:4501"},
    }
    config = Configuration.model_validate(_configuration() | {"environment": value})
    assert isinstance(config.environment, VMEnvironment)
    assert config.environment.executor is not None


if __name__ == "__main__":
    pytest_bazel.main()
