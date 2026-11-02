"""Executor retention permission and image-cache safety."""

import json
from pathlib import Path
from typing import Any

import pytest

from agentplane.system_tests.k3s import stack


@pytest.mark.parametrize(
    ("references", "container_refs"),
    [
        ({"repoTags": ["fixture:current"]}, []),
        ({"repoDigests": ["fixture@sha256:current"]}, []),
        ({"pinned": True}, []),
        ({}, ["sha256:protected"]),
    ],
)
def test_image_cleanup_preserves_referenced_pinned_and_container_used_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, references: dict[str, Any], container_refs: list[str]
) -> None:
    k3s = stack.K3s(tmp_path, tmp_path / "unused-k3s")
    removed: list[str] = []

    def crictl(*args: str, check: bool = True) -> str:
        match args:
            case ("images", "-o", "json"):
                images = [{"id": "sha256:protected", **references}]
                if not removed:
                    images.append({"id": "sha256:obsolete"})
                return json.dumps({"images": images})
            case ("ps", "-a", "-o", "json"):
                # Use the resolved ID rather than the requested tag, which can now name a newer image.
                return json.dumps(
                    {"containers": [{"imageRef": ref, "image": {"image": "fixture:current"}} for ref in container_refs]}
                )
            case ("rmi", *ids):
                removed.extend(ids)
                if check:
                    raise RuntimeError("image was concurrently collected")
                return "image was concurrently collected"
            case _:
                raise AssertionError(args)

    monkeypatch.setattr(k3s, "crictl", crictl)

    k3s.prune_unused_images()

    assert removed == ["sha256:obsolete"]


def test_image_cleanup_surfaces_a_failed_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    k3s = stack.K3s(tmp_path, tmp_path / "unused-k3s")

    def crictl(*args: str, check: bool = True) -> str:
        match args:
            case ("images", "-o", "json"):
                return json.dumps({"images": [{"id": "sha256:obsolete"}]})
            case ("ps", "-a", "-o", "json"):
                return json.dumps({"containers": []})
            case ("rmi", "sha256:obsolete"):
                if check:
                    raise RuntimeError("runtime removal failed")
                return "runtime removal failed"
            case _:
                raise AssertionError(args)

    monkeypatch.setattr(k3s, "crictl", crictl)

    with pytest.raises(RuntimeError, match="obsolete containerd images survived cleanup"):
        k3s.prune_unused_images()


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


@pytest.mark.parametrize("link", ["pod", "replica_set"])
@pytest.mark.parametrize("owner", [None, {"controller": False}, {"kind": "StatefulSet"}, {"uid": "unrelated-owner"}])
def test_ready_postgres_pod_ignores_unrelated_ready_pod(link: str, owner: dict[str, Any] | None) -> None:
    ready = _postgres_pod("ready", phase="Running", ready=True)
    unrelated = _postgres_pod("unrelated", phase="Running", ready=True, replica_set_uid="unrelated-replicaset")
    unrelated_replica_set = _postgres_replica_set("unrelated-replicaset")
    metadata = unrelated["metadata"] if link == "pod" else unrelated_replica_set["metadata"]
    if owner is None:
        metadata.pop("ownerReferences")
    else:
        metadata["ownerReferences"][0].update(owner)

    selected = stack._ready_fixture_postgres_pod(
        [unrelated, ready], [_postgres_replica_set("replicaset-1"), unrelated_replica_set], "deployment-1"
    )

    assert selected["metadata"]["uid"] == "ready"


def test_ready_postgres_pod_ignores_running_unready_candidate() -> None:
    pods = [_postgres_pod("unready", phase="Running", ready=False)]

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
