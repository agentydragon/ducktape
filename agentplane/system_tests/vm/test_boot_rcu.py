"""Behavioral bounds and UID selection for the opt-in boot observer."""

import json
import threading
from pathlib import Path

import pytest_bazel

from agentplane.system_tests.vm import boot_rcu


def test_launcher_selection_requires_exact_vmi_owner_uid() -> None:
    def kubectl(*_args: str, **kwargs: object) -> str:
        assert kwargs["timeout"] == boot_rcu.POD_QUERY_TIMEOUT_SECONDS
        return json.dumps(
            {
                "items": [
                    {"metadata": {"name": "other", "uid": "other-pod", "ownerReferences": [{"uid": "other-vmi"}]}},
                    {"metadata": {"name": "target", "uid": "target-pod", "ownerReferences": [{"uid": "target-vmi"}]}},
                ]
            }
        )

    assert boot_rcu._launcher_pod(kubectl, "scope", "target-vmi") == ("target", "target-pod")
    assert boot_rcu._launcher_pod(kubectl, "scope", "absent-vmi") is None


def test_qemu_sample_selects_exact_pod_cgroup_and_bounds_threads(tmp_path: Path, monkeypatch) -> None:
    uid = "6e8d7cc0-a974-4051-ba60-103c789005b1"
    for pid, pod in ((100, uid), (200, "other-pod-uid")):
        root = tmp_path / str(pid)
        root.mkdir()
        (root / "comm").write_text("qemu-kvm\n")
        (root / "cgroup").write_text(f"0::/kubepods.slice/pod{pod.replace('-', '_')}/compute\n")
        for tid in (pid, pid + 1):
            task = root / "task" / str(tid)
            task.mkdir(parents=True)
            (task / "comm").write_text(f"CPU {tid - pid}/TCG\n")
            (task / "stat").write_text(f"{tid} (CPU {tid - pid}/TCG) S " + "0 " * 11 + "7 8 " + "0 " * 30)
            (task / "schedstat").write_text("42 19 2\n")
            (task / "wchan").write_text("futex_wait_queue\n")
    monkeypatch.setattr(boot_rcu, "MAX_THREADS", 1)
    result = boot_rcu._sample_qemu(tmp_path, uid)
    assert result["pid"] == 100
    assert result["thread_count"] == 2
    assert result["threads_truncated"] is True
    assert isinstance(result["threads"], list)
    assert len(result["threads"]) == 1
    assert boot_rcu._sample_qemu(tmp_path, "absent") == {
        "matching_qemu_processes": 0,
        "process_scan_truncated": False,
        "process_scan_incomplete": False,
    }


def test_qemu_scan_reports_unreadable_process_as_incomplete(tmp_path: Path, monkeypatch) -> None:
    process = tmp_path / "100"
    process.mkdir()
    comm = process / "comm"
    comm.write_text("qemu-kvm\n")
    original = Path.read_text

    def unreadable(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == comm:
            raise PermissionError("denied")
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert boot_rcu._sample_qemu(tmp_path, "target") == {
        "matching_qemu_processes": 0,
        "process_scan_truncated": False,
        "process_scan_incomplete": True,
    }


def test_output_byte_cap_keeps_complete_jsonl_records(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "samples.jsonl"
    path.write_bytes(b"")
    monkeypatch.setattr(boot_rcu, "MAX_BYTES", 40)
    assert boot_rcu._append_bounded(path, {"x": "short"})
    assert not boot_rcu._append_bounded(path, {"x": "too-long-to-fit-twice"})
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"x": "short"}]


def test_collector_records_byte_limit_as_clean_bounded_stop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(boot_rcu, "MAX_BYTES", 0)
    destination = tmp_path / "boot-rcu-host.jsonl"
    stop, thread = boot_rcu.start(lambda *_args, **_kwargs: '{"items": []}', "scope", "vmi-uid", destination)
    thread.join(timeout=2)
    assert not thread.is_alive()
    boot_rcu.stop_capture(stop, thread)
    summary = json.loads((tmp_path / "boot-rcu-host-summary.json").read_text())
    assert summary["stop_reason"] == "max_bytes"
    assert summary["samples_written"] == 0
    assert summary["clean_exit"] is True
    assert destination.read_bytes() == b""
    assert (tmp_path / "boot-rcu-host-summary.json").stat().st_size <= boot_rcu.MAX_SUMMARY_BYTES


def test_collector_records_explicit_teardown_and_written_count(tmp_path: Path) -> None:
    entered = threading.Event()

    def kubectl(*_args: str, **_kwargs: object) -> str:
        entered.set()
        return '{"items": []}'

    destination = tmp_path / "boot-rcu-host.jsonl"
    stop, thread = boot_rcu.start(kubectl, "scope", "vmi-uid", destination)
    assert entered.wait(timeout=2)
    boot_rcu.stop_capture(stop, thread)
    summary = json.loads((tmp_path / "boot-rcu-host-summary.json").read_text())
    assert summary["stop_reason"] == "teardown"
    assert summary["clean_exit"] is True
    assert summary["samples_written"] == len(destination.read_text().splitlines())


if __name__ == "__main__":
    pytest_bazel.main()
