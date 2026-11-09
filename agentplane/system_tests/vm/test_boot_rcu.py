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
from typing import Any

import pytest_bazel
from python.runfiles import Runfiles

# gazelle:include_dep @pypi//protobuf
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.vm import boot_rcu


def _readiness_view(*, pod_namespace: str = "scope") -> environment_service_pb2.EnvironmentDescriptor:
    return environment_service_pb2.EnvironmentDescriptor(
        target=identity_pb2.EnvironmentTarget(namespace="scope", name="sandbox", uid="sandbox-uid"),
        kubernetes_diagnostics=environment_service_pb2.KubernetesDiagnostics(
            pod=environment_service_pb2.SandboxPod(
                name="virt-launcher-sandbox", namespace=pod_namespace, uid="launcher-pod-uid"
            )
        ),
    )


def test_readiness_failure_snapshot_uses_exact_launcher_uid(tmp_path: Path, monkeypatch) -> None:
    view = _readiness_view()
    sampled: list[tuple[Path, str]] = []

    def sample(proc_root: Path, pod_uid: str) -> dict[str, object]:
        sampled.append((proc_root, pod_uid))
        return {"matching_qemu_processes": 1, "pid": 123, "threads": [{"tid": 123, "state": "S"}]}

    monkeypatch.setattr(boot_rcu, "_sample_qemu", sample)
    path = boot_rcu.capture_readiness_failure(view, tmp_path)

    record = json.loads(path.read_text())
    assert sampled == [(Path("/proc"), "launcher-pod-uid")]
    assert record["capture_status"] == "sampled"
    assert record["sandbox"] == {"namespace": "scope", "name": "sandbox", "uid": "sandbox-uid"}
    assert record["launcher_pod"] == {"namespace": "scope", "name": "virt-launcher-sandbox", "uid": "launcher-pod-uid"}
    assert record["host_qemu"]["pid"] == 123


def test_readiness_failure_snapshot_rejects_replacement_namespace(tmp_path: Path, monkeypatch) -> None:
    def unexpected_sample(_proc_root: Path, _pod_uid: str) -> dict[str, object]:
        raise AssertionError("must not sample a Pod from a different namespace")

    monkeypatch.setattr(boot_rcu, "_sample_qemu", unexpected_sample)
    path = boot_rcu.capture_readiness_failure(_readiness_view(pod_namespace="replacement"), tmp_path)
    record = json.loads(path.read_text())

    assert record["capture_status"] == "skipped_pod_identity_mismatch"
    assert record["host_qemu"]["not_sampled"]


def test_readiness_failure_snapshot_preserves_zero_qemu_matches(tmp_path: Path, monkeypatch) -> None:
    sampled: list[str] = []

    def no_matching_qemu(_proc_root: Path, pod_uid: str) -> dict[str, object]:
        sampled.append(pod_uid)
        return {"matching_qemu_processes": 0, "process_scan_truncated": False, "process_scan_incomplete": False}

    monkeypatch.setattr(boot_rcu, "_sample_qemu", no_matching_qemu)
    path = boot_rcu.capture_readiness_failure(_readiness_view(), tmp_path)
    record = json.loads(path.read_text())

    assert sampled == ["launcher-pod-uid"]
    assert record["capture_status"] == "sampled"
    assert record["host_qemu"]["matching_qemu_processes"] == 0


def test_readiness_failure_snapshot_records_collector_error(tmp_path: Path, monkeypatch) -> None:
    def failed_sample(_proc_root: Path, _pod_uid: str) -> dict[str, object]:
        raise PermissionError("host proc denied")

    monkeypatch.setattr(boot_rcu, "_sample_qemu", failed_sample)
    record = json.loads(boot_rcu.capture_readiness_failure(_readiness_view(), tmp_path).read_text())

    assert record["capture_status"] == "collector_error"
    assert record["host_qemu"] == {"capture_error": "PermissionError", "message": "host proc denied"}


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


def _launcher_pod_json(*, pod_uid: str = "pod-uid", vmi_uid: str = "vmi-uid") -> dict[str, Any]:
    return {
        "metadata": {
            "namespace": "scope",
            "name": "virt-launcher-test",
            "uid": pod_uid,
            "labels": {
                "agentplane.allegedly.works/sandbox": "sandbox-a",
                "kubevirt.io/created-by": vmi_uid,
                "vmi.kubevirt.io/id": "test-vmi",
            },
            "ownerReferences": [{"kind": "VirtualMachineInstance", "name": "test-vmi", "uid": vmi_uid}],
        },
        "status": {"phase": "Running"},
    }


def test_namespace_launcher_identity_requires_sandbox_vmi_owner_and_kubevirt_labels() -> None:
    def kubectl(*args: str, **kwargs: object) -> str:
        assert args[0:3] == ("get", "pods", "-n")
        assert "kubevirt.io=virt-launcher" in args
        assert kwargs["timeout"] == boot_rcu.POD_QUERY_TIMEOUT_SECONDS
        valid = _launcher_pod_json()
        mismatched_created_by = _launcher_pod_json(pod_uid="pod-wrong")
        mismatched_created_by["metadata"]["labels"]["kubevirt.io/created-by"] = "other-vmi"
        return json.dumps({"items": [valid, mismatched_created_by]})

    launchers, rejected = boot_rcu._namespace_launchers(kubectl, "scope")
    assert [launcher["pod_uid"] for launcher in launchers] == ["pod-uid"]
    assert launchers[0]["vmi_uid"] == "vmi-uid"
    assert rejected == 1


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


def test_multi_launcher_qemu_sample_matches_exact_pod_uid(tmp_path: Path) -> None:
    uid = "6e8d7cc0-a974-4051-ba60-103c789005b1"
    process = tmp_path / "100"
    process.mkdir()
    (process / "comm").write_text("qemu-kvm\n")
    (process / "cgroup").write_text(f"0::/kubepods.slice/pod{uid.replace('-', '_')}/compute\n")
    task = process / "task" / "100"
    task.mkdir(parents=True)
    (task / "comm").write_text("CPU 0/TCG\n")
    (task / "stat").write_text("100 (CPU 0/TCG) S " + "0 " * 11 + "7 8 " + "0 " * 30)
    (task / "schedstat").write_text("42 19 2\n")
    (task / "wchan").write_text("futex_wait_queue\n")

    qemus, scan = boot_rcu._sample_qemus(tmp_path, [uid, "other"])
    assert qemus[uid]["pid"] == 100
    assert qemus[uid]["matching_qemu_processes"] == 1
    assert qemus["other"]["matching_qemu_processes"] == 0
    assert scan["process_scan_incomplete"] is False


def test_namespace_collector_records_exact_identity_and_teardown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(boot_rcu, "FULL_SUITE_MAX_SAMPLES", 2)
    monkeypatch.setattr(boot_rcu, "FULL_SUITE_INTERVAL_SECONDS", 0.01)
    destination = tmp_path / "boot-rcu-host" / "full-suite.jsonl"

    def kubectl(*_args: str, **_kwargs: object) -> str:
        return json.dumps({"items": [_launcher_pod_json()]})

    stop, thread = boot_rcu.start_namespace(kubectl, "scope", destination, proc_root=tmp_path / "empty-proc")
    thread.join(timeout=2)
    assert not thread.is_alive()
    boot_rcu.stop_capture(stop, thread)

    records = [json.loads(line) for line in destination.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["launchers"][0]["pod_uid"] == "pod-uid"
    assert records[0]["launchers"][0]["vmi_uid"] == "vmi-uid"
    assert records[0]["launchers"][0]["host_qemu"]["matching_qemu_processes"] == 0
    summary = json.loads(destination.with_name("full-suite-summary.json").read_text())
    assert summary["status"] == "observer_stopped"
    assert summary["collector_exited_cleanly"] is True
    assert summary["stop_reason"] == "sample_limit"
    assert summary["window_exhausted"] is True
    assert summary["pre_launch_poll_count"] == 1
    assert summary["first_launcher_observed_at_utc"] is not None
    assert summary["sampling_elapsed_seconds"] < boot_rcu.FULL_SUITE_WINDOW_SECONDS
    assert summary["pods"][0]["pod_uid"] == "pod-uid"
    assert summary["pods"][0]["samples"] == 2


def test_namespace_collector_preserves_api_error_as_incomplete_not_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(boot_rcu, "FULL_SUITE_MAX_SAMPLES", 2)
    monkeypatch.setattr(boot_rcu, "FULL_SUITE_INTERVAL_SECONDS", 0.01)
    destination = tmp_path / "full-suite.jsonl"
    calls = 0

    def failed_kubectl(*_args: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps({"items": [_launcher_pod_json()]})
        raise RuntimeError("API unavailable")

    stop, thread = boot_rcu.start_namespace(failed_kubectl, "scope", destination, proc_root=tmp_path)
    thread.join(timeout=2)
    assert not thread.is_alive()
    boot_rcu.stop_capture(stop, thread)
    records = [json.loads(line) for line in destination.read_text().splitlines()]
    assert records[0]["launchers"][0]["pod_uid"] == "pod-uid"
    assert records[1]["capture_error"] == "RuntimeError"
    summary = json.loads(destination.with_name("full-suite-summary.json").read_text())
    assert summary["query_failures"] == 1
    assert summary["pods"][0]["pod_uid"] == "pod-uid"


def test_namespace_collector_bounds_wait_for_first_launcher(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(boot_rcu, "FULL_SUITE_PRE_LAUNCH_SECONDS", 0.02)
    monkeypatch.setattr(boot_rcu, "FULL_SUITE_INTERVAL_SECONDS", 0.005)
    destination = tmp_path / "full-suite.jsonl"
    stop, thread = boot_rcu.start_namespace(
        lambda *_args, **_kwargs: '{"items": []}', "scope", destination, proc_root=tmp_path
    )
    thread.join(timeout=2)
    assert not thread.is_alive()
    boot_rcu.stop_capture(stop, thread)
    summary = json.loads(destination.with_name("full-suite-summary.json").read_text())
    assert summary["stop_reason"] == "first_launcher_timeout"
    assert summary["samples_written"] == 0
    assert summary["first_launcher_observed_at_utc"] is None
    assert destination.read_bytes() == b""


def test_guest_serial_summary_correlates_by_launcher_uid_and_reports_missing(tmp_path: Path) -> None:
    artifact_root = tmp_path / "vm-artifacts"
    pod_uid = "pod-uid"
    serial = artifact_root / "serial" / f"scope_virt-launcher-test_{pod_uid}" / "virt-serial0-log"
    serial.mkdir(parents=True)
    (serial / "virt-serial0-log.log").write_bytes(
        b"\n".join(
            [
                b"AGENTPLANE_BOOT_RCU_SAMPLE_BEGIN seq=0",
                b"AGENTPLANE_BOOT_RCU_SAMPLE_END seq=0",
                b"AGENTPLANE_BOOT_RCU_SAMPLE_BEGIN seq=1",
                b"AGENTPLANE_BOOT_RCU_SAMPLE_END seq=1",
                b"AGENTPLANE_BOOT_RCU_OBSERVER_COMPLETE samples=2",
            ]
        )
    )
    host_summary = artifact_root / "boot-rcu-host" / "full-suite-summary.json"
    host_summary.parent.mkdir(parents=True)
    host_summary.write_text(json.dumps({"pods": [{"pod_uid": pod_uid}, {"pod_uid": "missing-uid"}]}))

    report = boot_rcu.summarize_guest_serials(artifact_root, "scope", host_summary)
    rows = {row["pod_uid"]: row for row in json.loads(report.read_text())["rows"]}
    assert rows[pod_uid]["guest_observer_complete"] is True
    assert rows[pod_uid]["host_observed"] is True
    assert rows["missing-uid"]["status"] == "missing"
    assert rows["missing-uid"]["host_observed"] is True


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


def test_guest_window_argument_selects_360_bounded_samples(tmp_path: Path) -> None:
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
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=ttyS0 agentplane.boot-rcu-observe=1 agentplane.boot-rcu-observe-window=360\n")
    env = os.environ | {
        "AGENTPLANE_BOOT_RCU_BUSYBOX": str(busybox),
        "AGENTPLANE_BOOT_RCU_CMDLINE_PATH": str(cmdline),
        "AGENTPLANE_BOOT_RCU_INTERVAL": "0.001",
        "AGENTPLANE_BOOT_RCU_TMPDIR": str(tmp_path),
    }
    env.pop("AGENTPLANE_BOOT_RCU_SAMPLES", None)
    result = subprocess.run([str(busybox), "sh", str(script)], check=True, capture_output=True, env=env, timeout=45)
    serial = result.stdout.replace(b"\r\n", b"\n")
    assert serial.count(b"AGENTPLANE_BOOT_RCU_SAMPLE_BEGIN seq=") == 360
    assert b"AGENTPLANE_BOOT_RCU_OBSERVER_COMPLETE samples=360\n" in serial


if __name__ == "__main__":
    pytest_bazel.main()
