"""The shared file contract rejects partial and contradictory deployment policy."""

import json

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.env.config import CodexReadiness, Configuration, GuestResources, SystemdEnvironment


def _configuration() -> dict[str, object]:
    return {
        "version": 3,
        "environment": {"supervision": "pid1"},
        "state_dir": "/test/state",
        "initialization_cwd": "/test/workspaces",
        "control": {
            "listen_address": "127.0.0.1:8090",
            "security": {
                "mode": "mtls",
                "tls": {
                    "cert_file": "/run/test/tls.crt",
                    "key_file": "/run/test/tls.key",
                    "ca_file": "/run/test/ca.crt",
                },
            },
        },
        "ingress": {"target": "history.test:8081", "bearer": "ingress-placeholder"},
        "platform_instructions": "Test guidance.",
        "harness_environment": {"inherit": ["PATH"], "values": {"HOME": "/test/home"}},
        "codex": {"binary": "/test/codex", "base_url": "http://models.test/v1", "token_env": "TEST_PROVIDER_TOKEN"},
    }


@pytest.mark.parametrize("enabled", [None, False, True])
def test_codex_turn_diff_notifications_are_optional_and_codex_only(enabled: bool | None) -> None:
    values = _configuration()
    codex: dict[str, object] = {
        "binary": "/test/codex",
        "base_url": "http://models.test/v1",
        "token_env": "TEST_PROVIDER_TOKEN",
    }
    if enabled is not None:
        codex["turn_diff_notifications"] = enabled
    values["codex"] = codex
    config = Configuration.model_validate(values)
    assert config.codex is not None
    assert config.codex.turn_diff_notifications is enabled
    serialized = json.loads(config.model_dump_json(exclude_none=True))["codex"]
    assert serialized == codex

    values["claude"] = {**codex, "turn_diff_notifications": True}
    with pytest.raises(ValidationError):
        Configuration.model_validate(values)


def test_codex_readiness_serializes_explicit_values_and_omits_unset_fields() -> None:
    config = Configuration.model_validate(
        _configuration()
        | {
            "codex_readiness": {
                "exec_server_ready_timeout_ms": 60_000,
                "exec_server_probe_timeout_ms": 5_000,
                "exec_server_dependency_timeout_ms": 120_000,
            }
        }
    )
    serialized = json.loads(config.model_dump_json(exclude_none=True))

    assert serialized["codex_readiness"] == {
        "exec_server_ready_timeout_ms": 60_000,
        "exec_server_probe_timeout_ms": 5_000,
        "exec_server_dependency_timeout_ms": 120_000,
    }


@pytest.mark.parametrize(
    "field",
    [
        "exec_server_ready_timeout_ms",
        "exec_server_probe_timeout_ms",
        "exec_server_probe_interval_ms",
        "exec_server_dependency_timeout_ms",
        "app_server_start_timeout_ms",
        "app_server_initialize_timeout_ms",
    ],
)
def test_codex_readiness_rejects_nonpositive_or_out_of_range_windows(field: str) -> None:
    with pytest.raises(ValidationError):
        CodexReadiness.model_validate({field: 0})
    with pytest.raises(ValidationError):
        CodexReadiness.model_validate({field: 9_223_372_036_855})


def test_codex_readiness_accepts_the_largest_duration_representable_in_milliseconds() -> None:
    readiness = CodexReadiness(exec_server_ready_timeout_ms=9_223_372_036_854)

    assert readiness.exec_server_ready_timeout_ms == 9_223_372_036_854


def test_runner_control_accepts_explicit_insecure_mode_without_tls_files() -> None:
    values = _configuration()
    values["control"] = {"listen_address": "127.0.0.1:8090", "security": {"mode": "insecure"}}

    config = Configuration.model_validate(values)
    serialized = json.loads(config.model_dump_json(exclude_none=True))

    assert serialized["control"] == {"listen_address": "127.0.0.1:8090", "security": {"mode": "insecure"}}


def test_runner_accepts_model_config_target() -> None:
    endpoint = "model-config.test:8080"
    config = Configuration.model_validate(_configuration() | {"model_config_target": endpoint})
    assert json.loads(config.model_dump_json(exclude_none=True))["model_config_target"] == endpoint


@pytest.mark.parametrize(
    "updates",
    [
        {"version": 2},
        {"log_level": ""},
        {"log_level": "trace"},
        {"log_level": "fatal"},
        {"session_reconcile_concurrency": 0},
        {"session_reconcile_concurrency": -1},
        {"model_config_target": "host:notaport"},
        {"state_dir": "relative"},
        {"workspace_root": ""},
        {"harness_environment": {"inherit": ["NAME=value"]}},
        {"harness_environment": {"values": {"": "value"}}},
        {"codex": {"binary": "/test/codex"}},
        {
            "control": {
                "listen_address": "127.0.0.1:8090",
                "tls": {
                    "cert_file": "/run/test/tls.crt",
                    "key_file": "/run/test/tls.key",
                    "ca_file": "/run/test/ca.crt",
                },
            }
        },
        {
            "control": {
                "listen_address": "host:notaport",
                "security": {
                    "mode": "mtls",
                    "tls": {
                        "cert_file": "/run/test/tls.crt",
                        "key_file": "/run/test/tls.key",
                        "ca_file": "/run/test/ca.crt",
                    },
                },
            }
        },
        {
            "control": {
                "listen_address": "0.0.0.0:8090",
                "security": {
                    "mode": "mtls",
                    "tls": {
                        "cert_file": "relative.crt",
                        "key_file": "/run/test/tls.key",
                        "ca_file": "/run/test/ca.crt",
                    },
                },
            }
        },
        {"control": {"listen_address": "127.0.0.1:8090", "security": {"mode": "mtls"}}},
        {"control": {"listen_address": "127.0.0.1:8090", "security": {"mode": "insecure", "tls": {}}}},
        {"control": {"listen_address": "127.0.0.1:8090", "security": {"tls": {}}}},
        {"environment": {}},
        {"environment": {"supervision": "unknown"}},
        {"environment": {"supervision": "pid1", "console_autologin": True}},
        {
            "environment": {
                "supervision": "pid1",
                "executor": {
                    "url": "ws://127.0.0.1:4501",
                    "owner": {"kind": "companion", "socket": "/test/socket"},
                    "placement": "systemd",
                },
            }
        },
        {
            "environment": {
                "supervision": "pid1",
                "executor": {"url": "ws://127.0.0.1:4501", "owner": {"kind": "systemd"}, "placement": "direct"},
            }
        },
        {
            "environment": {
                "supervision": "pid1",
                "executor": {
                    "url": "ws://0.0.0.0:4501",
                    "owner": {"kind": "companion", "socket": "/test/socket"},
                    "placement": "direct",
                },
            }
        },
        {
            "environment": {
                "supervision": "pid1",
                "executor": {
                    "url": "ws://127.0.0.1:0",
                    "owner": {"kind": "companion", "socket": "/test/socket"},
                    "placement": "direct",
                },
            }
        },
        {
            "environment": {
                "supervision": "pid1",
                "executor": {
                    "url": "ws://127.0.0.1:4501?",
                    "owner": {"kind": "companion", "socket": "/test/socket"},
                    "placement": "direct",
                },
            }
        },
        {
            "environment": {
                "supervision": "pid1",
                "executor": {
                    "url": "ws://127.0.0.1:4501",
                    "owner": {"kind": "companion", "socket": "relative"},
                    "placement": "direct",
                },
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
        "supervision": "systemd",
        "resources": _resources(),
        "agent_uid": 107,
        "agent_gid": 107,
        "block_devices": ["/dev/test-workspace"],
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"resources": {**_resources(), "service_memory_max_bytes": 1_610_612_736}},
        {"resources": {**_resources(), "aggregate": _limits(memory=805_306_367, pids=768)}},
        {"executor": {"url": "ws://127.0.0.1:4501", "owner": {"kind": "systemd"}, "placement": "direct"}},
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
        "executor": {"url": "ws://127.0.0.1:4501", "owner": {"kind": "systemd"}, "placement": "direct"},
    }
    config = Configuration.model_validate(_configuration() | {"environment": value})
    assert isinstance(config.environment, SystemdEnvironment)
    assert config.environment.executor is not None
    assert config.environment.executor.placement == "direct"


@pytest.mark.parametrize("kind", ["pod", "vm"])
@pytest.mark.parametrize("updates", [{}, {"placement": None}, {"placement": ""}, {"placement": "unknown"}])
def test_executor_placement_must_be_explicit(kind: str, updates: dict[str, object]) -> None:
    executor = {"url": "ws://127.0.0.1:4501"} | updates
    environment: dict[str, object]
    if kind == "pod":
        environment = {
            "supervision": "pid1",
            "executor": executor | {"owner": {"kind": "companion", "socket": "/run/executor.sock"}},
        }
    else:
        environment = _vm_environment() | {
            "resources": {**_resources(), "executor": _limits(memory=67_108_864, pids=128)},
            "executor": executor | {"owner": {"kind": "systemd"}},
        }
    with pytest.raises(ValidationError, match="placement"):
        Configuration.model_validate(_configuration() | {"environment": environment})


@pytest.mark.parametrize(
    ("kind", "executor_updates", "error"),
    [
        ("pod", {}, "owner"),
        ("pod", {"owner": {"kind": "companion"}}, "socket"),
        ("pod", {"owner": {"kind": "companion", "socket": "relative"}}, "socket"),
        ("pod", {"owner": {"kind": "systemd"}}, "companion executor owner"),
        (
            "pod",
            {"owner": {"kind": "companion", "socket": "/run/executor.sock"}, "placement": "systemd"},
            "systemd owner",
        ),
        ("vm", {}, "owner"),
        ("vm", {"owner": {"kind": "systemd", "socket": None}}, "socket"),
        ("vm", {"owner": {"kind": "companion", "socket": "/run/executor.sock"}}, "systemd executor owner"),
    ],
)
def test_executor_owner_must_match_the_supervision_requirements(
    kind: str, executor_updates: dict[str, object], error: str
) -> None:
    environment: dict[str, object] = (
        {"supervision": "pid1"}
        if kind == "pod"
        else _vm_environment() | {"resources": {**_resources(), "executor": _limits(memory=67_108_864, pids=128)}}
    )
    environment["executor"] = {"url": "ws://127.0.0.1:4501", "placement": "direct"} | executor_updates
    with pytest.raises(ValidationError, match=error):
        Configuration.model_validate(_configuration() | {"environment": environment})


@pytest.mark.parametrize(
    ("placement", "implementation", "workload", "error"),
    [
        ("systemd", "native", _limits(memory=33_554_432, pids=127), None),
        ("systemd", "native", None, "budget must be supplied together"),
        ("direct", "native", _limits(memory=33_554_432, pids=127), "budget must be supplied together"),
        ("systemd", "codex", _limits(memory=33_554_432, pids=127), "requires the native implementation"),
        ("systemd", "native", _limits(memory=33_554_433, pids=127), "remaining service memory high"),
        ("systemd", "native", _limits(memory=33_554_432, pids=128), "leave room for Environment Runtime"),
        ("systemd", "native", _limits(memory=0, pids=127), "greater than 0"),
        (
            "systemd",
            "native",
            {**_limits(memory=33_554_432, pids=127), "cpu_quota_micros": 100_001},
            "fit within Environment Runtime service quota",
        ),
    ],
)
def test_vm_systemd_executor_requires_native_and_workload_headroom(
    placement: str, implementation: str, workload: dict[str, object] | None, error: str | None
) -> None:
    value = _configuration() | {
        "environment": _vm_environment()
        | {
            "resources": {
                **_resources(),
                "executor": _limits(memory=33_554_432, pids=128),
                "executor_workload": workload,
            },
            "executor": {
                "url": "ws://127.0.0.1:4501",
                "owner": {"kind": "systemd"},
                "implementation": implementation,
                "placement": placement,
            },
        }
    }
    if error is not None:
        with pytest.raises(ValidationError, match=error):
            Configuration.model_validate(value)
    else:
        encoded = json.loads(Configuration.model_validate(value).model_dump_json(exclude_none=True))
        assert encoded["environment"]["executor"]["placement"] == "systemd"
        assert encoded["environment"]["resources"]["executor_workload"] == workload


@pytest.mark.parametrize("kind", ["pod", "vm"])
def test_executor_settings(kind: str) -> None:
    executor = {"url": "ws://127.0.0.1:4501", "placement": "direct", "limits": {"native": {"max_sessions": 3}}}
    environment: dict[str, object]
    if kind == "pod":
        environment = {
            "supervision": "pid1",
            "executor": executor | {"owner": {"kind": "companion", "socket": "/run/executor.sock"}},
        }
    else:
        environment = _vm_environment() | {
            "resources": {**_resources(), "executor": _limits(memory=67_108_864, pids=128)},
            "executor": executor | {"owner": {"kind": "systemd"}},
        }
    config = Configuration.model_validate(_configuration() | {"environment": environment})
    assert config.environment.executor is not None
    assert config.environment.executor.limits is not None
    assert config.environment.executor.limits.native is not None
    assert config.environment.executor.limits.native.max_sessions == 3
    environment["executor"] = config.environment.executor.model_dump() | {"implementation": "codex"}
    with pytest.raises(ValidationError):
        Configuration.model_validate(_configuration() | {"environment": environment})


@pytest.mark.parametrize("level", [None, "debug", "info", "warn", "error"])
def test_log_level(level: str | None) -> None:
    config = Configuration.model_validate(_configuration() | {"log_level": level})
    assert config.log_level == level
    encoded = json.loads(config.model_dump_json(exclude_none=True))
    if level is None:
        assert "log_level" not in encoded
    else:
        assert encoded["log_level"] == level


if __name__ == "__main__":
    pytest_bazel.main()
