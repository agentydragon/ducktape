"""Retention requires the permission injected through BuildBuddy's exec properties."""

from pathlib import Path
from typing import Any

import pytest

from agentplane.system_tests.k3s import stack


def _postgres_pod(
    uid: str,
    *,
    phase: str,
    ready: bool,
    replica_set_uid: str = "replicaset-1",
    deleting: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": f"postgres-678b844849-{uid}",
        "uid": uid,
        "ownerReferences": [{"controller": True, "kind": "ReplicaSet", "uid": replica_set_uid}],
    }
    if deleting:
        metadata["deletionTimestamp"] = "2026-11-02T08:13:00Z"
    status: dict[str, Any] = {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}
    if reason is not None:
        status["reason"] = reason
    return {"metadata": metadata, "status": status}


def _postgres_replica_set(uid: str) -> dict[str, Any]:
    return {
        "metadata": {"uid": uid, "ownerReferences": [{"controller": True, "kind": "Deployment", "uid": "deployment-1"}]}
    }


def test_ready_postgres_pod_ignores_evicted_replicaset_attempts() -> None:
    pods = [_postgres_pod(f"evicted-{index}", phase="Failed", ready=False, reason="Evicted") for index in range(6)] + [
        _postgres_pod("ready", phase="Running", ready=True)
    ]

    pod = stack._ready_fixture_postgres_pod(pods, [_postgres_replica_set("replicaset-1")], "deployment-1")

    assert pod["metadata"]["uid"] == "ready"


def test_ready_postgres_pod_rejects_zero_candidates() -> None:
    pods = [_postgres_pod("evicted", phase="Failed", ready=False)]

    with pytest.raises(RuntimeError, match=r"expected one Ready, non-terminating fixture PostgreSQL Pod.*found 0"):
        stack._ready_fixture_postgres_pod(pods, [_postgres_replica_set("replicaset-1")], "deployment-1")


def test_ready_postgres_pod_rejects_ambiguous_candidates() -> None:
    pods = [
        _postgres_pod("ready-1", phase="Running", ready=True, replica_set_uid="replicaset-1"),
        _postgres_pod("ready-2", phase="Running", ready=True, replica_set_uid="replicaset-2"),
    ]
    replica_sets = [_postgres_replica_set("replicaset-1"), _postgres_replica_set("replicaset-2")]

    with pytest.raises(RuntimeError, match=r"expected one Ready, non-terminating fixture PostgreSQL Pod.*found 2"):
        stack._ready_fixture_postgres_pod(pods, replica_sets, "deployment-1")


def test_ready_postgres_pod_ignores_terminating_candidate() -> None:
    pods = [_postgres_pod("terminating", phase="Running", ready=True, deleting=True)]

    with pytest.raises(RuntimeError, match=r"expected one Ready, non-terminating fixture PostgreSQL Pod.*found 0"):
        stack._ready_fixture_postgres_pod(pods, [_postgres_replica_set("replicaset-1")], "deployment-1")


@pytest.mark.parametrize("permission", [None, "", "0", "true"])
def test_no_retention_without_executor_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, permission: str | None
) -> None:
    # Credentials and generic CI context are also present on developer machines and runners.
    monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
    monkeypatch.setenv("CI", "true")
    if permission is None:
        monkeypatch.delenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", raising=False)
    else:
        monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", permission)
    baseline = tmp_path / "baseline"
    monkeypatch.setattr(stack, "_BASELINE_ROOT", baseline)
    with pytest.raises(RuntimeError, match="requires BuildBuddy's executor persistence permission"):
        stack._baseline_open(tmp_path / "unused-k3s", "unused-fingerprint", layers_in_memory=True)
    assert not baseline.exists()


def test_retained_daemon_logs_are_bounded_and_survive_a_missing_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "k3s.log").write_text("earlier log\nrecent log\n")
    (baseline / "transitions.jsonl").write_text("older event\nnewer event\n")
    data = tmp_path / "data"
    containerd = data / "agent/containerd"
    containerd.mkdir(parents=True)
    (containerd / "containerd.log").write_text("containerd tail\n")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setattr(stack, "_BASELINE_ROOT", baseline)
    monkeypatch.setattr(stack, "_RETAINED_LOG_LIMIT", 11)

    stack._capture_retained_logs(data, outputs)

    assert (outputs / "k3s-retained.log").read_text() == "recent log\n"
    assert (outputs / "containerd-retained.log").read_text() == "inerd tail\n"
    assert (outputs / "baseline-transitions.jsonl").read_text() == "ewer event\n"
    assert not (outputs / "containerd-teardown-retained.log").exists()


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
