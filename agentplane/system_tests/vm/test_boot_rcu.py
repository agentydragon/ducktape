"""Behavioral bounds and UID selection for the opt-in boot observer."""

import json
import os
import pty
import re
import stat
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

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


def test_guest_records_preserve_exact_reporter_receipts_on_shared_tty(tmp_path: Path) -> None:
    runfiles = Runfiles.CreateOrRaise()
    script = runfiles.Rlocation(os.environ["BOOT_RCU_OBSERVER_RLOCATION"])
    busybox_tar = runfiles.Rlocation(os.environ["BOOT_RCU_BUSYBOX_RLOCATION"])
    assert script is not None
    assert busybox_tar is not None
    busybox = tmp_path / "busybox"
    with tarfile.open(busybox_tar) as archive:
        member = next(member for member in archive if member.name.endswith("bin/busybox"))
        source = archive.extractfile(member)
        assert source is not None
        busybox.write_bytes(source.read())
    busybox.chmod(0o755)
    env = os.environ | {
        "AGENTPLANE_BOOT_RCU_BUSYBOX": str(busybox),
        "AGENTPLANE_BOOT_RCU_SAMPLES": "20",
        "AGENTPLANE_BOOT_RCU_INTERVAL": "0.02",
        "AGENTPLANE_BOOT_RCU_TMPDIR": str(tmp_path),
    }
    master, slave = pty.openpty()
    reporter = os.dup(slave)
    output = bytearray()

    def drain() -> None:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                return  # Linux PTY reports EIO after the last slave closes.
            if not chunk:
                return
            output.extend(chunk)

    reader = threading.Thread(target=drain)
    reader.start()
    process = subprocess.Popen([str(busybox), "sh", str(script)], env=env, stdout=slave, stderr=subprocess.PIPE)
    os.close(slave)

    receipt = b"AGENTPLANE_BOOT_EVIDENCE_ACTIVATION_END\n"
    writer_errors: list[BaseException] = []

    def write_reporter() -> None:
        try:
            for _ in range(3000):
                assert os.write(reporter, receipt) == len(receipt)
                time.sleep(0.0001)
        except BaseException as error:
            writer_errors.append(error)
        finally:
            os.close(reporter)

    writer = threading.Thread(target=write_reporter)
    writer.start()
    try:
        scratch_dirs: list[Path] = []
        deadline = time.monotonic() + 2
        while not scratch_dirs and time.monotonic() < deadline:
            scratch_dirs = list(tmp_path.glob("agentplane-boot-rcu.*"))
            time.sleep(0.001)
        assert len(scratch_dirs) == 1
        assert stat.S_IMODE(scratch_dirs[0].stat().st_mode) == 0o700
        status = process.wait(timeout=20)
        if status != 0:
            error_output = process.stderr.read().decode(errors="replace") if process.stderr is not None else ""
            raise AssertionError(f"observer exited {status}: {error_output}")
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert not writer_errors
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        writer.join(timeout=5)
        reader.join(timeout=5)
        os.close(master)

    serial = bytes(output).replace(b"\r\n", b"\n")
    assert not list(tmp_path.glob("agentplane-boot-rcu.*"))
    lines = serial.splitlines()
    assert lines.count(receipt.rstrip(b"\n")) == 3000
    for sample in range(20):
        begin = f"AGENTPLANE_BOOT_RCU_SAMPLE_BEGIN seq={sample}".encode()
        end = f"AGENTPLANE_BOOT_RCU_SAMPLE_END seq={sample}".encode()
        assert lines.count(begin) == lines.count(end) == 1
        record = re.search(re.escape(begin) + rb"\n(.*?)" + re.escape(end) + rb"\n", serial, re.DOTALL)
        assert record is not None
        assert len(record.group(0)) <= 2048
        assert receipt.rstrip(b"\n") not in record.group(0)
        assert b"AGENTPLANE_BOOT_RCU_SAMPLE_ERROR" not in record.group(0)
    assert lines.count(b"AGENTPLANE_BOOT_RCU_OBSERVER_COMPLETE samples=20") == 1


if __name__ == "__main__":
    pytest_bazel.main()
