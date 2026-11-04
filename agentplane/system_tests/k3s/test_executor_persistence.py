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


@pytest.mark.parametrize(
    ("references", "container_refs"),
    [
        ({"repoTags": ["fixture:current"]}, []),
        ({"repoDigests": ["fixture@sha256:current"]}, []),
        ({"pinned": True}, []),
        ({}, ["sha256:importing"]),
    ],
)
def test_image_cleanup_accepts_a_new_reference_during_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, references: dict[str, Any], container_refs: list[str]
) -> None:
    k3s = stack.K3s(tmp_path, tmp_path / "unused-k3s")
    removed: list[str] = []

    def crictl(*args: str, check: bool = True) -> str:
        match args:
            case ("images", "-o", "json"):
                return json.dumps({"images": [{"id": "sha256:importing", **(references if removed else {})}]})
            case ("ps", "-a", "-o", "json"):
                return json.dumps({"containers": [{"imageRef": ref} for ref in container_refs] if removed else []})
            case ("rmi", *ids):
                removed.extend(ids)
                return ""
            case _:
                raise AssertionError(args)

    monkeypatch.setattr(k3s, "crictl", crictl)

    k3s.prune_unused_images()

    assert removed == ["sha256:importing"]


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


def _action_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    workspace = tmp_path / "action-workspace"
    test_tmpdir = workspace / "_tmp/test"
    outputs_dir = workspace / "bazel-out/k8-fastbuild/testlogs/example/test.outputs"
    test_setup = workspace / "external/bazel_tools/tools/test/test-setup.sh"
    test_tmpdir.mkdir(parents=True)
    outputs_dir.mkdir(parents=True)
    test_setup.parent.mkdir(parents=True)
    test_setup.touch()
    monkeypatch.setenv("TEST_TMPDIR", str(test_tmpdir))
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(outputs_dir))
    return workspace


def test_executor_workspace_requires_permission_and_declared_test_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _action_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")

    assert stack._executor_action_workspace_root() == workspace

    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "0")
    assert stack._executor_action_workspace_root() is None

    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    (workspace / "external/bazel_tools/tools/test/test-setup.sh").unlink()
    assert stack._executor_action_workspace_root() is None


def test_snapshot_invalidation_marker_uses_action_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = _action_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    stack._write_executor_snapshot_invalidation_marker()
    assert (workspace / stack._INVALIDATE_SNAPSHOT_MARKER).is_file()


def test_persistent_stack_error_invalidates_snapshot_and_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _action_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    lifecycle_error = RuntimeError("baseline cleanup failed")

    with pytest.raises(RuntimeError) as caught, stack._invalidate_snapshot_on_action_error(enabled=True):
        raise lifecycle_error

    assert caught.value is lifecycle_error
    assert (workspace / stack._INVALIDATE_SNAPSHOT_MARKER).is_file()


def test_persistent_stack_success_does_not_invalidate_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = _action_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    with stack._invalidate_snapshot_on_action_error(enabled=True):
        pass
    assert not (workspace / stack._INVALIDATE_SNAPSHOT_MARKER).exists()


def test_nonpersistent_stack_error_does_not_invalidate_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _action_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    with (
        pytest.raises(RuntimeError, match="ordinary stack failed"),
        stack._invalidate_snapshot_on_action_error(enabled=False),
    ):
        raise RuntimeError("ordinary stack failed")
    assert not (workspace / stack._INVALIDATE_SNAPSHOT_MARKER).exists()


def test_persistent_stack_error_without_executor_permission_does_not_invalidate_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _action_workspace(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", raising=False)
    with (
        pytest.raises(RuntimeError, match="persistent stack failed"),
        stack._invalidate_snapshot_on_action_error(enabled=True),
    ):
        raise RuntimeError("persistent stack failed")
    assert not (workspace / stack._INVALIDATE_SNAPSHOT_MARKER).exists()


def test_base_exception_invalidates_persistent_executor_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _action_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    abort = KeyboardInterrupt("interrupted stack setup")
    with pytest.raises(KeyboardInterrupt) as caught, stack._invalidate_snapshot_on_action_error(enabled=True):
        raise abort
    assert caught.value is abort
    assert (workspace / stack._INVALIDATE_SNAPSHOT_MARKER).is_file()


def test_marker_write_failure_keeps_original_error_and_adds_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _action_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    lifecycle_error = RuntimeError("baseline teardown failed")

    def fail_marker_write() -> None:
        raise OSError("workspace marker is read-only")

    monkeypatch.setattr(stack, "_write_executor_snapshot_invalidation_marker", fail_marker_write)
    with pytest.raises(RuntimeError) as caught, stack._invalidate_snapshot_on_action_error(enabled=True):
        raise lifecycle_error

    assert caught.value is lifecycle_error
    assert any("workspace marker is read-only" in note for note in lifecycle_error.__notes__)


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
