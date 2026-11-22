"""Exercise the KVM worker's live demand query and shadow recommendation."""

from typing import Any, cast

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.testing.manifests import find_resource
from cluster.cdk8s.testing.promtool import PromtoolRules

pytest_plugins = ("cluster.cdk8s.testing.promtool",)

_WORKER_MANIFEST = "ducktape/cluster/k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml"
_SHADOW_TARGET = "buildbuddy_kvm:shadow_target_workers"
_HEALTH_CHECKS = ("registered_to_scheduler", "grpc_app_connection", "grpc_cache_connection", "executor_disk_usage")
_NAMESPACE = 'namespace="buildbuddy-executor"'
_POOL = "buildbuddy-kvm-worker"
_WORK_DISK_THRESHOLD = 50 * 1024**3


def _worker_rules(runfiles: Runfiles) -> dict[str, Any]:
    manifest = (runfiles.root() / _WORKER_MANIFEST).read_text()
    rule = find_resource(yaml.safe_load_all(manifest), "PrometheusRule", _POOL)
    return cast(dict[str, Any], rule["spec"])


def test_demand_query(runfiles: Runfiles, promtool: PromtoolRules) -> None:
    cluster = runfiles.root() / "ducktape/cluster"
    autoscaler = cluster / "generated/buildbuddy-kvm-worker-autoscaler/buildbuddy-kvm-worker-autoscaler.k8s.yaml"
    scaled_object = find_resource(yaml.safe_load_all(autoscaler.read_text()), "ScaledObject", _POOL)
    query = scaled_object["spec"]["triggers"][0]["metadata"]["query"]
    promtool(
        "ducktape/cluster/cdk8s/testdata/buildbuddy_scaling_rules.yaml",
        {
            "buildbuddy.yaml": {
                "groups": [
                    {"name": "demand", "rules": [{"record": "buildbuddy_kvm:demand", "expr": query}]},
                    *_worker_rules(runfiles)["groups"],
                ]
            }
        },
    )


def _series(name: str, samples: int | str) -> dict[str, str]:
    # Cases evaluate at 4m. Integer inputs have a fresh sample every minute.
    return {"series": name, "values": f"{samples}+0x4" if isinstance(samples, int) else samples}


def _worker(ordinal: int, **overrides: int | str | None) -> list[dict[str, str]]:
    pod = f'{_NAMESPACE},pod="virt-launcher-{_POOL}-{ordinal}-test"'
    target = f'{pod},service="{_POOL}-metrics"'
    executor = f'{target},endpoint="metrics"'
    guest = f'{target},endpoint="node-metrics"'
    execution = "buildbuddy_remote_execution"
    inputs: dict[str, tuple[str, int | None]] = {
        "pod": (
            f'kube_pod_info{{{pod},created_by_kind="VirtualMachineInstance",created_by_name="{_POOL}-{ordinal}"}}',
            1,
        ),
        "deleting": (f"kube_pod_deletion_timestamp{{{pod}}}", None),
        "executor_up": (f"up{{{executor}}}", 1),
        "guest_up": (f"up{{{guest}}}", 1),
        **{
            check: (f'buildbuddy_health_check_status{{{executor},health_check_name="{check}"}}', 1)
            for check in _HEALTH_CHECKS
        },
        "assigned_cpu": (f"{execution}_assigned_milli_cpu{{{executor}}}", 0),
        "assignable_cpu": (f"{execution}_assignable_milli_cpu{{{executor}}}", 4_000),
        "assigned_ram": (f"{execution}_assigned_ram_bytes{{{executor}}}", 0),
        "assignable_ram": (f"{execution}_assignable_ram_bytes{{{executor}}}", 7 * 1024**3),
        "work_disk": (f'node_filesystem_avail_bytes{{{guest},mountpoint="/buildbuddy"}}', _WORK_DISK_THRESHOLD),
        "root_disk": (f'node_filesystem_avail_bytes{{{guest},mountpoint="/"}}', 60_000_000_000),
        "queue": (f'{execution}_queue_length{{{executor},group_id="test-group"}}', None),
    }
    return [
        _series(name, samples)
        for signal, (name, default) in inputs.items()
        if (samples := overrides.get(signal, default)) is not None
    ]


def _shadow_case(
    name: str, *workers: list[dict[str, str]], desired: int | str | None, expected: int | None
) -> dict[str, Any]:
    series = [sample for worker in workers for sample in worker]
    if desired is not None:
        series.append(
            _series(f'kube_customresource_kubevirt_vmpool_desired_replicas{{{_NAMESPACE},name="{_POOL}"}}', desired)
        )
    return {
        "name": name,
        "interval": "1m",
        "input_series": series,
        "promql_expr_test": [
            {
                "expr": _SHADOW_TARGET,
                "eval_time": "4m",
                "exp_samples": [] if expected is None else [{"labels": _SHADOW_TARGET, "value": expected}],
            }
        ],
    }


def test_shadow_target(runfiles: Runfiles, promtool: PromtoolRules) -> None:
    idle = [_worker(i) for i in range(3)]
    tests = [
        _shadow_case("no desired state produces no target", *idle, desired=None, expected=None),
        _shadow_case("desired state 180 seconds old produces no target", *idle, desired="3 3", expected=None),
        _shadow_case("idle target respects the floor", *idle, desired=3, expected=2),
        _shadow_case(
            "ignore a queue sample 180 seconds old", *idle[:2], _worker(2, queue="4 4"), desired=3, expected=2
        ),
        _shadow_case(
            "ignore reservations from a deleting launcher",
            *idle,
            _worker(3, deleting=1, queue=4),
            desired=3,
            expected=2,
        ),
        _shadow_case(
            "count each busy worker once even when a new idle worker has no queue series",
            _worker(0, assigned_cpu=4_000, queue=0),
            _worker(1, assigned_cpu=3_000),
            _worker(2, assigned_cpu=1_000),
            _worker(3),
            desired=4,
            expected=3,
        ),
        _shadow_case(
            "hold when a different worker can fit the full reservation at the thresholds",
            _worker(0, assigned_cpu=4_000, queue=4),
            _worker(1, assigned_ram=1),
            _worker(2),
            desired=3,
            expected=3,
        ),
        _shadow_case(
            "add one when each worker is short of a different resource",
            _worker(0, assigned_cpu=1, queue=4),
            _worker(1, assigned_ram=1),
            _worker(2, work_disk=_WORK_DISK_THRESHOLD - 1),
            desired=3,
            expected=4,
        ),
        _shadow_case(
            "failed connection and disk checks disqualify otherwise fitting workers",
            _worker(0, assigned_cpu=4_000, queue=4),
            _worker(1, grpc_app_connection=0),
            _worker(2, grpc_cache_connection=0),
            _worker(3, executor_disk_usage=0),
            desired=4,
            expected=5,
        ),
        _shadow_case(
            "an unsatisfied queue at the cap keeps eight workers",
            *[_worker(i, assigned_cpu=4_000, queue=4) for i in range(8)],
            desired=8,
            expected=8,
        ),
        _shadow_case(
            "hold when a requested worker is missing even with an unsatisfied queue",
            _worker(0, assigned_cpu=4_000, queue=4),
            _worker(1, assigned_cpu=4_000),
            desired=3,
            expected=3,
        ),
    ]
    incomplete_worker: dict[str, dict[str, int | str | None]] = {
        "executor scrape fails": {"executor_up": 0},
        "guest scrape fails": {"guest_up": 0},
        "registration fails": {"registered_to_scheduler": 0},
        "launcher is deleting": {"deleting": 1},
        "launcher identity is missing": {"pod": None},
        "launcher identity is 180 seconds old": {"pod": "1 1"},
        "work disk sample is 180 seconds old": {"work_disk": f"{_WORK_DISK_THRESHOLD} {_WORK_DISK_THRESHOLD}"},
        **{
            f"{signal} is missing": {signal: None}
            for signal in (
                *_HEALTH_CHECKS,
                "assigned_cpu",
                "assignable_cpu",
                "assigned_ram",
                "assignable_ram",
                "work_disk",
            )
        },
    }
    tests.extend(
        _shadow_case(f"hold when {reason}", *idle[:2], _worker(2, **overrides), desired=3, expected=3)
        for reason, overrides in incomplete_worker.items()
    )
    promtool(
        {"rule_files": ["worker.yaml"], "evaluation_interval": "1m", "tests": tests},
        {"worker.yaml": _worker_rules(runfiles)},
    )


if __name__ == "__main__":
    pytest_bazel.main()
