"""Retained-node compatibility and ownership gates without starting Kubernetes."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentplane.system_tests.k3s import baseline


@pytest.fixture
def retained_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "retained"
    root.mkdir()
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", root)
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", root / "server.json")
    monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", "1")
    return root


@pytest.mark.parametrize("changed", ["binary", "crd", "release", "image", "helper", "layer_mode", "data_directory"])
def test_infrastructure_inputs_change_compatibility(
    tmp_path: Path, changed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        case "data_directory":
            monkeypatch.setenv("AGENTPLANE_K3S_DATA_DIR", str(tmp_path / "runner-private"))
    assert fingerprint(layers_in_memory=changed != "layer_mode") != previous


def test_relative_data_directory_cannot_reset_an_action_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENTPLANE_K3S_DATA_DIR", "relative")
    with pytest.raises(ValueError, match="must be absolute"):
        baseline.K3s(tmp_path, tmp_path / "k3s")


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


def _fake_containerd_profiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> baseline.K3s:
    binary = tmp_path / "fake-k3s"
    binary.write_text(f"#!{sys.executable}\n{source}")
    binary.chmod(0o700)
    debug_socket = tmp_path / "containerd-debug.sock"
    monkeypatch.setattr(baseline.Path, "is_socket", lambda _path: True)
    monkeypatch.setattr(baseline, "CONTAINERD_DEBUG_SOCKET", str(debug_socket))
    return baseline.K3s(tmp_path, binary)


def test_profile_containerd_records_complete_bounded_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = _fake_containerd_profiler(tmp_path, monkeypatch, "import sys\nsys.stdout.buffer.write(b'profile')\n")
    destination = tmp_path / "profile.pprof"

    with node.profile_containerd(
        "profile", 1, destination, max_output_bytes=64, phase_span_name="system_test.stack.setup"
    ) as capture:
        phase_start = time.time_ns()
        capture.record_phase_start(phase_start, "image import queue started")
        capture.record_phase_end(time.time_ns(), "image import queue finished")

    metadata = json.loads(destination.with_suffix(".pprof.json").read_text())
    assert destination.read_bytes() == b"profile"
    assert metadata["status"] == "complete"
    assert metadata["complete"] is True
    assert metadata["phase_within_requested_client_window"] is True
    assert metadata["phase_sample_start_acknowledged"] is False
    assert "server sampling start" in metadata["phase_coverage_basis"]
    assert metadata["context_exit_wait_seconds"] is not None


def test_profile_containerd_truncates_at_output_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = _fake_containerd_profiler(tmp_path, monkeypatch, "import sys\nsys.stdout.buffer.write(b'x' * 32)\n")
    destination = tmp_path / "trace"

    with node.profile_containerd("trace", 1, destination, max_output_bytes=8):
        pass

    metadata = json.loads(destination.with_suffix(".json").read_text())
    assert destination.read_bytes() == b"x" * 8
    assert metadata["status"] == "truncated"
    assert metadata["truncated"] is True
    assert metadata["response_bytes"] == 32


def test_profile_containerd_marks_cancelled_capture_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = tmp_path / "ready"
    source = (
        "import pathlib, signal, sys, time\n"
        "sys.stdout.buffer.write(b'partial')\n"
        "sys.stdout.flush()\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
        "while True: time.sleep(1)\n"
    )
    node = _fake_containerd_profiler(tmp_path, monkeypatch, source)
    destination = tmp_path / "trace"

    def cancel_capture() -> None:
        with node.profile_containerd("trace", 60, destination, max_output_bytes=1024) as capture:
            capture.record_phase_start(time.time_ns(), "image import queue started")
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() >= deadline:
                    pytest.fail("fake profiler did not start")
                time.sleep(0.01)
            raise RuntimeError("stop capture")

    with pytest.raises(RuntimeError, match="stop capture"):
        cancel_capture()

    metadata = json.loads(destination.with_suffix(".json").read_text())
    assert destination.read_bytes() == b"partial"
    assert metadata["status"] == "cancelled"
    assert metadata["complete"] is False
    assert metadata["phase_within_requested_client_window"] is False
