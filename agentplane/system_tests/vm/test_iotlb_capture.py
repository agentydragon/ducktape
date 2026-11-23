"""Bounded history and exact-domain selection for the IOTLB observer."""

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentplane.system_tests.vm import iotlb_capture
from agentplane.system_tests.vm.iotlb_profile import Profile


def process(proc: Path, pid: int, *, domain: str, uid: str) -> Path:
    path = proc / str(pid)
    (path / "task" / str(pid)).mkdir(parents=True)
    (path / "comm").write_text("qemu-kvm\n")
    (path / "cmdline").write_bytes(f"qemu-kvm\0-name\0guest={domain},debug-threads=on\0".encode())
    (path / "cgroup").write_text(f"0::/kubepods/burstable/pod{uid}/compute\n")
    (path / "stat").write_text(f"{pid} (CPU 0/KVM) " + " ".join(["S"] + ["0"] * 18 + ["12345"]))
    return path


def event(name: str, *, tid: int = 101, fields: bytes = b"cpu=0x1234") -> bytes:
    # Linux 5.15 trace_pipe payload shape; event names do not include the group.
    return f" CPU 0/TCG-{tid} [001] ..... 3.869000: {name}: (0x1234) ".encode() + fields + b"\n"


def test_selects_exact_domain_and_pod_uid_not_helper_qemus(tmp_path: Path) -> None:
    launcher = tmp_path / "testns_virt-launcher-test-vm-abcde_aaaaaaaa-bbbb"
    proc = tmp_path / "proc"
    process(proc, 101, domain="testns_test-vm-extra", uid="aaaaaaaa-bbbb")
    process(proc, 102, domain="testns_test-vm", uid="aaaaaaaa-bbbb-extra")
    process(proc, 103, domain="testns_test-vm", uid="aaaaaaaa_bbbb")
    target = iotlb_capture.find_process(launcher, "testns", proc)
    assert target is not None
    assert target.process == proc / "103"
    assert target.domain == "testns_test-vm"
    assert target.start_time == "12345"
    process(proc, 104, domain="testns_test-vm", uid="aaaaaaaa-bbbb")
    with pytest.raises(RuntimeError, match="Multiple"):
        iotlb_capture.find_process(launcher, "testns", proc)


def test_history_retains_failure_after_overwrite_and_accounts_for_gaps(tmp_path: Path) -> None:
    history = iotlb_capture.History()
    history.append(event("failure"), {101})
    for _ in range(100):
        history.append(event("check", fields=b"cpu=0x1234 padding=" + b"x" * 16000), {101})
    history.append(event("check", tid=102), {101})
    history.append(event("check", fields=b"dispatch=0x0 sections_nb=0"), {101})
    history.append(b"corrupt\n", {101})
    history.save(tmp_path)
    counters = history.counters()
    assert history.overwritten_records > 0
    assert counters["foreign_records"] == 1
    assert counters["malformed_records"] == 1
    assert counters["fetch_faults_or_invalid_values"] == 1
    assert b"failure:" in (tmp_path / "failure.log").read_bytes()
    assert b"failure:" not in (tmp_path / "rolling.log").read_bytes()
    assert (tmp_path / "rolling.log").stat().st_size <= iotlb_capture.ROLLING_BYTES
    assert (tmp_path / "failure.log").stat().st_size <= iotlb_capture.FAILURE_BYTES
    size = sum(path.stat().st_size for path in tmp_path.iterdir()) + iotlb_capture.METADATA_BYTES
    assert size <= iotlb_capture.LAUNCHER_BYTES
    assert size * iotlb_capture.MAX_LAUNCHERS + iotlb_capture.METADATA_BYTES <= iotlb_capture.TOTAL_BYTES


def test_partial_pipe_reads_preserve_records_and_report_actual_progress() -> None:
    history = iotlb_capture.History()
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb", buffering=0) as writer:
        line = event("check")
        writer.write(line[:20])
        pending, progress = iotlb_capture.drain(reader.fileno(), history, {101}, b"")
        assert progress == 20
        assert history.sequence == 0
        writer.write(line[20:] + event("check", tid=999))
        pending, progress = iotlb_capture.drain(reader.fileno(), history, {101}, pending)
        assert progress > 0
        assert not pending
        assert history.sequence == 1
        assert history.foreign == 1


def test_probe_setup_failure_is_recorded_without_failing_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = iotlb_capture.Target("test-launcher", "test-uid", "test-domain", tmp_path / "101", "1")
    profile = Profile(build_id="test", supplementary_build_id="test", binary_sha256="test", probes=[])

    @contextmanager
    def denied(*args: object, **kwargs: object) -> Iterator[iotlb_capture.ActiveTrace]:
        if args:
            raise PermissionError("test tracefs denial")
        yield iotlb_capture.ActiveTrace(tmp_path, tmp_path, tmp_path, tmp_path)

    monkeypatch.setattr(iotlb_capture, "probes", denied)
    output = tmp_path / "outputs"
    iotlb_capture.capture_one(target, profile, output, threading.Event())
    metadata = json.loads((output / "metadata.json").read_bytes())
    assert metadata["attached"] is None
    assert metadata["stop_reason"] == "setup-failed"
    assert "collector-error" in metadata["gaps"]
    assert metadata["errors"]
    assert not (output / "rolling.log").read_bytes()


def test_assertion_ring_survives_history_loss_and_stops_both_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history_instance = tmp_path / "history"
    failure_instance = tmp_path / "failure"
    for path in (history_instance, failure_instance):
        path.mkdir()
        (path / "tracing_on").write_text("1\n")
    (history_instance / "trace_pipe").write_bytes(event("fill_io") + event("check"))
    (failure_instance / "trace_pipe").write_bytes(event("failure", fields=b"cpu=0x1234 index=5 sections_nb=2"))
    target = iotlb_capture.Target("test-launcher", "test-uid", "test-domain", tmp_path / "101", "1")
    profile = Profile(build_id="test", supplementary_build_id="test", binary_sha256="test", probes=[])

    @contextmanager
    def attached(
        target: iotlb_capture.Target, profile: Profile, record: iotlb_capture.Record, **kwargs: object
    ) -> Iterator[iotlb_capture.ActiveTrace]:
        record.threads = [101]
        record.attached = 1.0
        try:
            yield iotlb_capture.ActiveTrace(history_instance, tmp_path, failure_instance, tmp_path)
        finally:
            record.trace_stats = {"history/cpu0": {"overrun": 42}}

    monkeypatch.setattr(iotlb_capture, "probes", attached)
    output = tmp_path / "outputs"
    iotlb_capture.capture_one(target, profile, output, threading.Event())
    metadata = json.loads((output / "metadata.json").read_bytes())
    assert metadata["stop_reason"] == "assertion-observed"
    assert metadata["failure_observed"]
    assert "kernel-events-lost" in metadata["gaps"]
    assert "failure-to-userspace-stop-window" in metadata["gaps"]
    assert b"index=5 sections_nb=2" in (output / "failure.log").read_bytes()
    assert b"fill_io:" in (output / "rolling.log").read_bytes()
    assert all((path / "tracing_on").read_text().strip() == "0" for path in (history_instance, failure_instance))


def test_discovery_loads_verified_manifest_and_joins_on_teardown(tmp_path: Path) -> None:
    with iotlb_capture.capture("testns", tmp_path):
        pass
    metadata = json.loads((tmp_path / "host-qemu" / "iotlb-observer.json").read_bytes())
    assert metadata["launchers"] == []
    assert metadata["errors"] == []
    assert not metadata["discovery_expired"]
