"""Retained-node compatibility and ownership gates without starting Kubernetes."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.k3s import baseline


@pytest.fixture
def retained_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "retained"
    root.mkdir()
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", root)
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", root / "server.json")
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    return root


@pytest.mark.parametrize(
    ("permission", "require_kvm", "private"),
    [(None, "1", False), ("0", "1", False), ("1", "0", False), ("1", "1", True)],
)
def test_persistent_data_dir_is_private_only_for_authorized_kvm(
    retained_root: Path, monkeypatch: pytest.MonkeyPatch, permission: str | None, require_kvm: str, private: bool
) -> None:
    if permission is None:
        monkeypatch.delenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", raising=False)
    else:
        monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", permission)
    monkeypatch.setenv("AGENTPLANE_K3S_REQUIRE_KVM", require_kvm)

    expected = retained_root / "data" if private else Path("/var/lib/k3s-env")
    assert baseline._persistent_data_dir() == expected


def test_open_baseline_records_private_kvm_data_path(
    retained_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENTPLANE_K3S_REQUIRE_KVM", "1")
    source = tmp_path / "k3s-source"
    source.write_bytes(b"test k3s binary")

    class FakeK3s:
        def __init__(self, work: Path, binary: Path, *, data_dir: Path) -> None:
            self.work = work
            self.binary = binary
            self.data_dir = data_dir

        def start(self, *, layers_in_memory: bool, detached: bool) -> Any:
            assert not layers_in_memory
            assert detached
            return type("Process", (), {"pid": 123})()

        def stop(self, _process: Any) -> None:
            raise AssertionError("published marker should transfer process ownership")

    monkeypatch.setattr(baseline, "K3s", FakeK3s)
    monkeypatch.setattr(baseline, "_baseline_identity", lambda _k3s, _pid: {"pid": 123, "test": "owned"})

    k3s, marker, warm = baseline.open_baseline(source, "private-kvm-fingerprint", layers_in_memory=False)

    expected = retained_root / "data"
    assert not warm
    assert k3s.data_dir == expected
    assert marker["data_dir"] == str(expected)
    assert json.loads((retained_root / "server.json").read_text())["data_dir"] == str(expected)


@pytest.mark.parametrize(
    "existing",
    ["marker", "marker_symlink", "marker_temp", "data_dir", "data_symlink", "data_dangling_symlink", "root_symlink"],
)
def test_fresh_kvm_data_dir_refuses_existing_state_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    root = tmp_path / "retained"
    root.mkdir()
    marker = root / "server.json"
    data_parent = tmp_path / "action-test-tmp"
    data_parent.mkdir()
    data_dir = data_parent / "k3s"
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", root)
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", marker)
    if existing == "marker":
        marker.write_text('{"state":"dirty"}')
    elif existing == "marker_symlink":
        marker.symlink_to(tmp_path / "missing-marker-target")
    elif existing == "marker_temp":
        marker.with_suffix(".tmp").write_text('{"state":"dirty"}')
    elif existing == "data_dir":
        data_dir.mkdir()
    elif existing == "data_symlink":
        data_dir.symlink_to(tmp_path)
    elif existing == "data_dangling_symlink":
        data_dir.symlink_to(tmp_path / "missing-data-target")
    elif existing == "root_symlink":
        root.rmdir()
        root.symlink_to(tmp_path / "other-root", target_is_directory=True)
    else:
        raise AssertionError(existing)

    with pytest.raises(RuntimeError, match="fresh KVM action found"):
        baseline.fresh_kvm_data_dir(data_parent=data_parent)

    if existing == "root_symlink":
        assert root.is_symlink()
    elif existing.startswith("marker"):
        marker_path = marker.with_suffix(".tmp") if existing == "marker_temp" else marker
        assert os.path.lexists(marker_path)
        if existing == "marker":
            assert marker.read_text() == '{"state":"dirty"}'
    else:
        assert os.path.lexists(data_dir)


def test_fresh_kvm_data_dir_uses_explicit_action_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_parent = tmp_path / "action-test-tmp"
    data_parent.mkdir()
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", tmp_path / "retained")
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", tmp_path / "retained/server.json")

    data_dir = baseline.fresh_kvm_data_dir(data_parent=data_parent)

    assert data_dir == data_parent / "k3s"
    assert not data_dir.exists()
    assert not (tmp_path / "retained").exists()


@pytest.mark.parametrize("invalid_parent", ["relative", "missing", "symlink"])
def test_fresh_kvm_data_dir_requires_a_real_explicit_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_parent: str
) -> None:
    parent = tmp_path / "action-test-tmp"
    if invalid_parent == "symlink":
        target = tmp_path / "actual-action-test-tmp"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
        data_parent = parent
    elif invalid_parent == "missing":
        data_parent = parent
    else:
        data_parent = Path("relative-action-test-tmp")
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", tmp_path / "retained")
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", tmp_path / "retained/server.json")

    with pytest.raises(RuntimeError, match="fresh KVM action"):
        baseline.fresh_kvm_data_dir(data_parent=data_parent)

    assert not (tmp_path / "retained").exists()
    if invalid_parent == "symlink":
        assert parent.is_symlink()


@pytest.mark.parametrize("private", [False, True])
def test_stop_previous_uses_only_marker_owned_data_path(
    retained_root: Path, monkeypatch: pytest.MonkeyPatch, private: bool
) -> None:
    expected = retained_root / "data" if private else Path("/var/lib/k3s-env")
    marker: dict[str, Any] = {"state": "clean", "fingerprint": "owned", "identity": {"pid": 123}}
    if private:
        marker["data_dir"] = str(expected)
    baseline.write_marker(marker)
    constructed = []
    stopped = []

    class FakeK3s:
        def __init__(self, work: Path, binary: Path, *, data_dir: Path) -> None:
            self.data_dir = data_dir
            constructed.append(data_dir)

    monkeypatch.setattr(baseline, "K3s", FakeK3s)
    monkeypatch.setattr(baseline, "stop_owned", lambda node, saved: stopped.append((node, saved)))

    baseline.stop_previous()

    assert constructed == [expected]
    assert stopped[0][1] == marker
    assert not (retained_root / "server.json").exists()


def test_stop_previous_rejects_unowned_data_path(retained_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = {"state": "clean", "fingerprint": "other", "identity": {"pid": 123}, "data_dir": "/tmp/other"}
    baseline.write_marker(marker)
    called = []
    monkeypatch.setattr(baseline, "stop_owned", lambda *_args: called.append(True))

    with pytest.raises(RuntimeError, match="unowned K3s data directory"):
        baseline.stop_previous()

    assert not called
    assert (retained_root / "server.json").exists()


def test_stop_owned_refuses_a_marker_for_another_data_path(retained_root: Path) -> None:
    node = baseline.K3s(retained_root, retained_root / "unused-binary")
    previous = {"identity": {"pid": os.getpid()}, "data_dir": str(retained_root / "data")}

    with pytest.raises(RuntimeError, match="data directory does not match"):
        baseline.stop_owned(node, previous)


@pytest.mark.parametrize("changed", ["binary", "crd", "release", "image", "helper", "layer_mode"])
def test_infrastructure_inputs_change_compatibility(tmp_path: Path, changed: str) -> None:
    binary = tmp_path / "k3s"
    crd = tmp_path / "crd.yaml"
    helper = tmp_path / "extension.py"
    layout = tmp_path / "image"
    layout.mkdir()
    for path in (binary, crd, helper, layout / "index.json"):
        path.write_text("before")
    releases: list[dict[str, Any]] = [{"kind": "Deployment", "metadata": {"name": "test-controller"}}]

    def fingerprint(*, layers_in_memory: bool = True) -> str:
        return baseline.fingerprint(
            binary,
            [crd],
            releases,
            {"test-infra:unset": layout},
            layers_in_memory=layers_in_memory,
            helpers={"extension.py": helper},
        )

    previous = fingerprint()
    match changed:
        case "binary" | "crd" | "helper" | "image":
            {"binary": binary, "crd": crd, "helper": helper, "image": layout / "index.json"}[changed].write_text(
                "after"
            )
        case "release":
            releases[0]["spec"] = {"replicas": 2}
        case "layer_mode":
            pass
    assert fingerprint(layers_in_memory=changed != "layer_mode") != previous


def test_fingerprint_is_stable_across_input_order(tmp_path: Path) -> None:
    binary = tmp_path / "k3s"
    binary.write_bytes(b"test binary")
    layouts = {}
    helpers = {}
    for name in ("a", "b"):
        layout = tmp_path / name
        layout.mkdir()
        (layout / "index.json").write_text(name)
        layouts[name] = layout
        helpers[name] = layout / "index.json"
    first = baseline.fingerprint(binary, [], [], layouts, layers_in_memory=True, helpers=helpers)
    assert (
        baseline.fingerprint(
            binary,
            [],
            [],
            dict(reversed(layouts.items())),
            layers_in_memory=True,
            helpers=dict(reversed(helpers.items())),
        )
        == first
    )


@pytest.mark.parametrize("marker", [{}, {"state": "unknown"}])
def test_invalid_marker_is_rejected_before_ownership_fields_are_read(
    retained_root: Path, marker: dict[str, str]
) -> None:
    baseline.write_marker(marker)
    with pytest.raises(RuntimeError, match="invalid persistence state marker"):
        baseline.open_baseline(retained_root / "unused-binary", "compatible", layers_in_memory=True)


def test_matching_clean_baseline_is_marked_dirty_before_reuse(
    retained_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {"pid": os.getpid(), "test": "owned-identity"}
    baseline.write_marker({"state": "clean", "fingerprint": "compatible", "identity": identity})
    monkeypatch.setattr(baseline, "_baseline_identity", lambda _node, _pid: identity)

    _, marker, warm = baseline.open_baseline(retained_root / "unused-binary", "compatible", layers_in_memory=True)

    assert warm
    assert marker["state"] == "dirty"
    assert json.loads((retained_root / "server.json").read_text())["state"] == "dirty"


@pytest.mark.parametrize(("state", "fingerprint"), [("dirty", "compatible"), ("clean", "different")])
def test_dirty_or_incompatible_baseline_cannot_attach(
    retained_root: Path, monkeypatch: pytest.MonkeyPatch, state: str, fingerprint: str
) -> None:
    baseline.write_marker({"state": state, "fingerprint": fingerprint, "identity": {"pid": 123}})
    stopped = []

    def stop_owned(_node: baseline.K3s, marker: dict[str, Any]) -> None:
        stopped.append(marker)
        raise RuntimeError("test stops at cold rebuild boundary")

    monkeypatch.setattr(baseline, "stop_owned", stop_owned)
    with pytest.raises(RuntimeError, match="cold rebuild boundary"):
        baseline.open_baseline(retained_root / "unused-binary", "compatible", layers_in_memory=True)
    assert stopped[0]["state"] == state


def test_unhealthy_clean_baseline_cannot_attach(retained_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline.write_marker({"state": "clean", "fingerprint": "compatible", "identity": {"pid": 123}})
    stopped = []

    def unhealthy(_node: baseline.K3s, _pid: int) -> dict[str, str | int]:
        raise RuntimeError("API unavailable")

    def stop_owned(_node: baseline.K3s, marker: dict[str, Any]) -> None:
        stopped.append(marker)
        raise RuntimeError("test stops at cold rebuild boundary")

    monkeypatch.setattr(baseline, "_baseline_identity", unhealthy)
    monkeypatch.setattr(baseline, "stop_owned", stop_owned)
    with pytest.raises(RuntimeError, match="cold rebuild boundary"):
        baseline.open_baseline(retained_root / "unused-binary", "compatible", layers_in_memory=True)
    assert len(stopped) == 1


def test_reused_pid_cannot_be_stopped(retained_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = baseline.K3s(retained_root, retained_root / "unused-binary")
    monkeypatch.setattr(baseline, "_baseline_process_start", lambda _pid: "new-start-ticks")
    previous = {
        "identity": {
            "pid": os.getpid(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "process_start_ticks": "old-start-ticks",
        }
    }
    with pytest.raises(RuntimeError, match="PID was reused"):
        baseline.stop_owned(node, previous)


@pytest.mark.parametrize(
    ("previous_binary", "state", "free_bytes", "retainable"),
    [
        (None, "clean", baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES, False),
        (b"test-k3s", "clean", baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES, True),
        (b"test-k3s", "dirty", baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES, False),
        (b"other-k3s", "clean", baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES, False),
        (b"test-k3s", "clean", baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES - 1, False),
    ],
)
def test_image_store_requires_compatible_clean_state_and_disk_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_binary: bytes | None,
    state: str,
    free_bytes: int,
    retainable: bool,
) -> None:
    binary = tmp_path / "test-k3s"
    binary.write_bytes(b"test-k3s")
    node = baseline.K3s(tmp_path, binary)
    node.data_dir = tmp_path / "k3s-env"
    marker = node.data_dir / "agent/containerd/.stack-state.json"
    marker.parent.mkdir(parents=True)
    if previous_binary is not None:
        marker.write_text(json.dumps({"state": state, "binary_sha256": hashlib.sha256(previous_binary).hexdigest()}))
    monkeypatch.setattr(baseline.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free_bytes))
    assert node.image_store_is_retainable() is retainable


if __name__ == "__main__":
    pytest_bazel.main()
