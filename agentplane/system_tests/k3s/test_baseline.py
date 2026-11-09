"""Retained-node compatibility and ownership gates without starting Kubernetes."""

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
    ("unclean_stop", "free_bytes", "retainable"),
    [
        (False, baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES, True),
        (True, baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES, False),
        (False, baseline.RETAINED_IMAGE_STORE_MIN_FREE_BYTES - 1, False),
    ],
)
def test_image_store_is_dropped_after_unclean_stop_or_low_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unclean_stop: bool, free_bytes: int, retainable: bool
) -> None:
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    node.data_dir = tmp_path / "k3s-env"
    marker = node.data_dir / "agent/containerd/.stack-running"
    marker.parent.mkdir(parents=True)
    if unclean_stop:
        marker.touch()
    monkeypatch.setattr(baseline.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free_bytes))
    assert node.image_store_is_retainable() is retainable


if __name__ == "__main__":
    pytest_bazel.main()
