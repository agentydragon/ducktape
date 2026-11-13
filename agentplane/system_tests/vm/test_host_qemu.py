"""Host-side shutdown diagnostics stay scoped and cannot fail the VM scenario."""

import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.vm import host_qemu


def test_shutdown_markers_are_exact_and_namespace_scoped(tmp_path: Path) -> None:
    serial = tmp_path / "serial"
    stalled = serial / "systest_virt-launcher-one_uid-stalled" / "vmi-one" / "serial.log"
    complete = serial / "systest_virt-launcher-two_uid-complete" / "vmi-two" / "serial.log"
    foreign = serial / "other_virt-launcher-three_uid-foreign" / "vmi-three" / "serial.log"
    for path, contents in (
        (
            stalled,
            b"prefix AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\n"
            + host_qemu.SHUTDOWN_REQUESTED
            + b"\r\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE_EXTRA\n",
        ),
        (complete, host_qemu.SHUTDOWN_REQUESTED + b"\n" + host_qemu.SHUTDOWN_COMPLETE + b"\n"),
        (foreign, host_qemu.SHUTDOWN_REQUESTED + b"\n"),
    ):
        path.parent.mkdir(parents=True)
        path.write_bytes(contents)

    launchers = host_qemu._namespace_launchers(serial, "systest")
    markers = {launcher: host_qemu._launcher_markers(launcher) for launcher in launchers}

    assert launchers == [stalled.parents[1], complete.parents[1]]
    assert markers == {stalled.parents[1]: (True, False), complete.parents[1]: (True, True)}
    assert [path for path, (requested, proof) in markers.items() if requested and not proof] == [stalled.parents[1]]


def test_host_qemu_processes_are_matched_by_pod_cgroup_and_keep_nspid(tmp_path: Path) -> None:
    pod_uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    proc = tmp_path / "proc"
    for pid, uid in ((101, pod_uid.replace("-", "_")), (202, "ffffffff-1111-2222-3333-444444444444")):
        process = proc / str(pid)
        task = process / "task" / str(pid)
        task.mkdir(parents=True)
        (process / "comm").write_text("qemu-kvm\n")
        (process / "cgroup").write_text(f"0::/kubepods/burstable/pod{uid}/container\n")
        (process / "cmdline").write_bytes(b"/usr/bin/qemu-kvm\0-name\0guest=test\0")
        (process / "status").write_text(f"Name:\tqemu-kvm\nNSpid:\t{pid}\t15\n")
        for field, value in {
            "comm": "qemu-kvm\n",
            "status": f"Name:\tqemu-kvm\nNSpid:\t{pid}\t15\n",
            "stat": f"{pid} (qemu-kvm) S 1\n",
            "wchan": "futex_wait_queue_me\n",
            "syscall": "202 0x0\n",
            "stack": "[<0>] futex_wait_queue_me\n",
            "schedstat": "10 20 3\n",
        }.items():
            (task / field).write_text(value)

    output = io.StringIO()
    host_qemu._snapshot_host_qemu(output, pod_uid, threading.Event(), time.monotonic() + 2, proc_root=proc)

    snapshot = output.getvalue()
    assert "HOST_QEMU_PID 101" in snapshot
    assert "HOST_QEMU_HOST_TID 101" in snapshot
    assert "HOST_QEMU_PID 202" not in snapshot
    assert "NSpid:\t101\t15" in snapshot
    assert "STAT: 101 (qemu-kvm) S 1" in snapshot
    assert "WCHAN: futex_wait_queue_me" in snapshot
    assert "SYSCALL: 202 0x0" in snapshot
    assert "STACK: [<0>] futex_wait_queue_me" in snapshot
    assert "SCHEDSTAT: 10 20 3" in snapshot


def _fake_virtiofs_process(proc: Path, pid: int, cgroup: str, *, comm: str = "virtiofsd", tids: int = 1) -> None:
    process = proc / str(pid)
    process.mkdir(parents=True)
    (process / "comm").write_text(comm + "\n")
    (process / "cgroup").write_text(cgroup + "\n")
    (process / "cmdline").write_bytes(b"virtiofsd\0--secret=PRIVATE_ARGUMENT\0")
    for field, value in {
        "status": f"Name:\t{comm}\nNSpid:\t{pid}\t1\n",
        "wchan": "request_wait_answer\n",
        "syscall": "202 0x1\n",
        "stack": "[<0>] fuse_sync_fs\n",
    }.items():
        (process / field).write_text(value)
    for tid in range(pid, pid + tids):
        task = process / "task" / str(tid)
        task.mkdir(parents=True)
        for field in ("comm", "status", "wchan", "syscall", "stack"):
            (task / field).write_bytes((process / field).read_bytes())


def test_virtiofs_processes_match_exact_pod_uid_and_multiple_daemons(tmp_path: Path) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    other = "ffffffff-1111-2222-3333-444444444444"
    proc = tmp_path / "proc"
    _fake_virtiofs_process(proc, 101, f"0::/kubepods/burstable/pod{uid}/container", tids=2)
    _fake_virtiofs_process(
        proc, 201, f"0::/kubepods.slice/kubepods-burstable-pod{uid.replace('-', '_')}.slice/container"
    )
    _fake_virtiofs_process(proc, 301, f"0::/kubepods/burstable/pod{uid}extra/container")
    _fake_virtiofs_process(proc, 401, f"0::/kubepods/burstable/pod{other}/container")
    _fake_virtiofs_process(proc, 501, f"0::/kubepods/burstable/pod{uid}/container", comm="qemu-kvm")

    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(output, uid, threading.Event(), time.monotonic() + 2, proc_root=proc)

    snapshot = output.getvalue()
    assert any(line.startswith("HOST_VIRTIOFSD_PID 101 ") for line in snapshot.splitlines())
    assert any(line.startswith("HOST_VIRTIOFSD_PID 201 ") for line in snapshot.splitlines())
    assert not any(line.startswith("HOST_VIRTIOFSD_PID 301 ") for line in snapshot.splitlines())
    assert not any(line.startswith("HOST_VIRTIOFSD_PID 401 ") for line in snapshot.splitlines())
    assert not any(line.startswith("HOST_VIRTIOFSD_PID 501 ") for line in snapshot.splitlines())
    assert "HOST_VIRTIOFSD_HOST_TID 102\n" in snapshot
    assert "NSpid:\t101\t1" in snapshot
    assert "WCHAN: request_wait_answer" in snapshot
    assert "STACK: [<0>] fuse_sync_fs" in snapshot
    assert f"scanned=4 matching_pod=2 threads=3 pod_uid={uid}" in snapshot
    assert "PRIVATE_ARGUMENT" not in snapshot


@pytest.mark.parametrize(("limit", "marker"), [("threads", "thread-limit"), ("processes", "process-limit")])
def test_virtiofs_process_snapshot_enforces_count_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, marker: str
) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    proc = tmp_path / "proc"
    _fake_virtiofs_process(proc, 101, f"0::/kubepods/pod{uid}/container", tids=3)
    _fake_virtiofs_process(proc, 201, f"0::/kubepods/pod{uid}/container")
    if limit == "threads":
        monkeypatch.setattr(host_qemu, "VIRTIOFSD_MAX_THREADS", 2)
    else:
        monkeypatch.setattr(host_qemu, "VIRTIOFSD_MAX_PROCESSES", 1)

    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(output, uid, threading.Event(), time.monotonic() + 2, proc_root=proc)

    snapshot = output.getvalue()
    lines = snapshot.splitlines()
    assert f"HOST_VIRTIOFSD_SCAN_STOPPED reason={marker}" in snapshot
    if limit == "threads":
        assert sum(line.startswith("HOST_VIRTIOFSD_HOST_TID ") for line in lines) == 2
        assert "threads=2" in next(line for line in lines if line.startswith("HOST_VIRTIOFSD_PROCESS_SCAN "))
    else:
        assert sum(line.startswith("HOST_VIRTIOFSD_PID ") for line in lines) == 1
        assert "matching_pod=1" in next(line for line in lines if line.startswith("HOST_VIRTIOFSD_PROCESS_SCAN "))


def test_virtiofs_process_snapshot_stops_between_proc_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    proc = tmp_path / "proc"
    _fake_virtiofs_process(proc, 101, f"0::/kubepods/pod{uid}/container")
    stop = threading.Event()
    original = host_qemu._read_proc_field

    def stop_after_first_read(output: io.StringIO, path: Path, field: str) -> None:
        original(output, path, field)
        stop.set()

    monkeypatch.setattr(host_qemu, "_read_proc_field", stop_after_first_read)
    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(output, uid, stop, time.monotonic() + 2, proc_root=proc)

    snapshot = output.getvalue()
    assert any(line.startswith("HOST_VIRTIOFSD_PID 101 ") for line in snapshot.splitlines())
    assert "HOST_VIRTIOFSD_SCAN_STOPPED reason=budget-or-stop" in snapshot
    assert "STATUS:" not in snapshot
    assert "HOST_VIRTIOFSD_HOST_TID" not in snapshot


def test_virtiofs_process_snapshot_distinguishes_absence_permission_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    proc = tmp_path / "proc"
    _fake_virtiofs_process(proc, 101, f"0::/kubepods/pod{uid}/container")
    read_text = Path.read_text
    read_bytes = Path.read_bytes

    def restricted_text(path: Path) -> str:
        if path == proc / "101" / "cgroup":
            raise PermissionError("cgroup denied")
        return read_text(path)

    monkeypatch.setattr(Path, "read_text", restricted_text)
    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(output, uid, threading.Event(), time.monotonic() + 2, proc_root=proc)
    assert "HOST_VIRTIOFSD_PROCESS_ERROR pid=101 type=PermissionError" in output.getvalue()
    assert "HOST_VIRTIOFSD_PROCESS_MATCH_UNCERTAIN" in output.getvalue()
    monkeypatch.setattr(Path, "read_text", read_text)

    def restricted_bytes(path: Path) -> bytes:
        if path == proc / "101" / "task" / "101" / "stack":
            raise PermissionError("kernel stack denied")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", restricted_bytes)
    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(output, uid, threading.Event(), time.monotonic() + 2, proc_root=proc)
    assert any(line.startswith("HOST_VIRTIOFSD_PID 101 ") for line in output.getvalue().splitlines())
    assert "STACK_ERROR PermissionError" in output.getvalue()
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(
        output, "different-pod-uid", threading.Event(), time.monotonic() + 2, proc_root=proc
    )
    assert "HOST_VIRTIOFSD_PROCESS_MATCH_NONE pod_uid=different-pod-uid" in output.getvalue()

    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(output, uid, threading.Event(), time.monotonic() - 1, proc_root=proc)
    assert output.getvalue() == "HOST_VIRTIOFSD_SKIPPED budget-or-stop\n"


def test_ready_baseline_is_bounded_and_arms_exact_projected_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    container_id = "a" * 64
    container_names_by_id = {container_id: "virtiofs-state"}

    def record(
        output: Any, pod_uid: str, stop: threading.Event, deadline: float, *, container_names_by_id: dict[str, str]
    ) -> tuple[int, bool]:
        assert pod_uid == uid
        assert container_names_by_id == {container_id: "virtiofs-state"}
        output.write("HOST_VIRTIOFSD_PROCESS_SCAN scanned=3 matching_pod=3 threads=24\n")
        return 3, True

    monkeypatch.setattr(host_qemu, "_snapshot_host_virtiofsd", record)
    artifacts = tmp_path / "vm-artifacts"
    assert host_qemu.record_virtiofsd_ready_baseline(
        artifacts, "systest", "virt-launcher-agentplane-vm-123", uid, container_names_by_id
    )

    root = artifacts / "host-qemu"
    target = json.loads((root / host_qemu.VIRTIOFSD_TIMELINE_TARGET).read_text())
    expected_launcher = f"systest_virt-launcher-agentplane-vm-123_{uid}"
    assert target == {
        "baseline": next((root / expected_launcher).glob("virtiofsd-baseline-*.txt")).name,
        "launcher": expected_launcher,
        "namespace": "systest",
        "pod": "virt-launcher-agentplane-vm-123",
        "pod_uid": uid,
        "container_names_by_id": container_names_by_id,
    }
    baseline = root / expected_launcher / target["baseline"]
    assert baseline.stat().st_size <= host_qemu.VIRTIOFSD_TIMELINE_MAX_BYTES
    assert "phase=ready-baseline" in baseline.read_text()
    assert "matching_pod=3" in baseline.read_text()
    assert "qmp=disabled" in baseline.read_text()


def test_virtiofs_container_map_includes_regular_and_restartable_init_containers() -> None:
    state = "a" * 64
    trust = "b" * 64
    tls = "c" * 64
    assert host_qemu.virtiofs_container_names_by_id(
        {
            "containerStatuses": [
                {"name": "virtiofs-trust", "containerID": f"containerd://{trust}"},
                {"name": "compute", "containerID": f"containerd://{'d' * 64}"},
            ],
            "initContainerStatuses": [
                {"name": "virtiofs-state", "containerID": f"containerd://{state}"},
                {"name": "virtiofs-tls", "containerID": f"containerd://{tls}"},
                {"name": "virtiofs-invalid", "containerID": "containerd://short"},
            ],
        }
    ) == {state: "virtiofs-state", trust: "virtiofs-trust", tls: "virtiofs-tls"}


def test_virtiofs_snapshot_labels_pid_with_exact_cgroup_container_id(tmp_path: Path) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    container_id = "a" * 64
    proc = tmp_path / "proc"
    _fake_virtiofs_process(proc, 101, f"0::/kubepods/pod{uid}/cri-containerd-{container_id}.scope")
    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(
        output,
        uid,
        threading.Event(),
        time.monotonic() + 2,
        proc_root=proc,
        container_names_by_id={container_id: "virtiofs-state"},
    )
    assert f"HOST_VIRTIOFSD_PID 101 container_id={container_id} container_name=virtiofs-state" in output.getvalue()


def test_virtiofs_snapshot_labels_unmapped_and_ambiguous_container_ids(tmp_path: Path) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    proc = tmp_path / "proc"
    _fake_virtiofs_process(proc, 101, f"0::/kubepods/pod{uid}/container")
    _fake_virtiofs_process(proc, 201, f"0::/kubepods/pod{uid}/{'a' * 64}/{'b' * 64}")
    output = io.StringIO()
    host_qemu._snapshot_host_virtiofsd(output, uid, threading.Event(), time.monotonic() + 2, proc_root=proc)
    assert "HOST_VIRTIOFSD_PID 101 container_id=unavailable container_name=unmapped" in output.getvalue()
    assert "HOST_VIRTIOFSD_PID 201 container_id=ambiguous container_name=unmapped" in output.getvalue()


def test_virtiofs_timeline_requires_complete_absence_and_stops_after_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    artifacts = tmp_path / "vm-artifacts"

    def baseline(output: Any, *_args: Any, **_kwargs: Any) -> tuple[int, bool]:
        output.write("baseline\n")
        return 1, True

    monkeypatch.setattr(host_qemu, "_snapshot_host_virtiofsd", baseline)
    assert host_qemu.record_virtiofsd_ready_baseline(artifacts, "systest", "virt-launcher-agentplane-vm-123", uid)
    launcher = artifacts / "serial" / f"systest_virt-launcher-agentplane-vm-123_{uid}" / "vmi"
    launcher.mkdir(parents=True)
    (launcher / "serial.log").write_bytes(host_qemu.SHUTDOWN_REQUESTED + b"\n" + host_qemu.SHUTDOWN_COMPLETE + b"\n")
    monkeypatch.setattr(host_qemu, "VIRTIOFSD_TIMELINE_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(host_qemu, "VIRTIOFSD_TIMELINE_MAX_SECONDS", 0.1)
    samples = iter(((2, True), (0, True), (0, True)))
    observed: list[int] = []

    def take(_artifacts: Path, _launcher: Path, index: int, *_args: object, **_kwargs: object) -> tuple[int, bool]:
        observed.append(index)
        return next(samples)

    monkeypatch.setattr(host_qemu, "_take_virtiofsd_timeline_sample", take)
    host_qemu._observe_virtiofsd_timeline(artifacts, "systest", threading.Event())

    summary = (artifacts / "host-qemu" / launcher.parent.name / "virtiofsd-timeline-summary.txt").read_text()
    assert observed == [0, 1, 2]
    assert "post_acpi_samples=3" in summary
    assert "stop_reason=proof-and-daemon-absent" in summary


def test_virtiofs_timeline_does_not_claim_absence_from_incomplete_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    artifacts = tmp_path / "vm-artifacts"
    monkeypatch.setattr(host_qemu, "_snapshot_host_virtiofsd", lambda output, *_args, **_kwargs: (1, True))
    assert host_qemu.record_virtiofsd_ready_baseline(artifacts, "systest", "virt-launcher-agentplane-vm-123", uid)
    launcher_dir = artifacts / "serial" / f"systest_virt-launcher-agentplane-vm-123_{uid}"
    serial = launcher_dir / "vmi" / "serial.log"
    serial.parent.mkdir(parents=True)
    serial.write_bytes(host_qemu.SHUTDOWN_REQUESTED + b"\n" + host_qemu.SHUTDOWN_COMPLETE + b"\n")
    monkeypatch.setattr(host_qemu, "VIRTIOFSD_TIMELINE_MAX_SAMPLES", 2)
    monkeypatch.setattr(host_qemu, "_take_virtiofsd_timeline_sample", lambda *_args, **_kwargs: (0, False))

    host_qemu._observe_virtiofsd_timeline(artifacts, "systest", threading.Event())

    summary = (artifacts / "host-qemu" / launcher_dir.name / "virtiofsd-timeline-summary.txt").read_text()
    assert "post_acpi_samples=2" in summary
    assert "stop_reason=sample-limit" in summary


def test_virtiofs_timeline_labels_capture_teardown_separately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    artifacts = tmp_path / "vm-artifacts"
    monkeypatch.setattr(host_qemu, "_snapshot_host_virtiofsd", lambda *_args, **_kwargs: (1, True))
    assert host_qemu.record_virtiofsd_ready_baseline(artifacts, "systest", "virt-launcher-agentplane-vm-123", uid)
    launcher_dir = artifacts / "serial" / f"systest_virt-launcher-agentplane-vm-123_{uid}"
    serial = launcher_dir / "vmi" / "serial.log"
    serial.parent.mkdir(parents=True)
    serial.write_bytes(host_qemu.SHUTDOWN_REQUESTED + b"\n")
    stop = threading.Event()

    def take(*_args: Any, **_kwargs: Any) -> tuple[int, bool]:
        stop.set()
        return 1, False

    monkeypatch.setattr(host_qemu, "_take_virtiofsd_timeline_sample", take)
    host_qemu._observe_virtiofsd_timeline(artifacts, "systest", stop)

    summary = (artifacts / "host-qemu" / launcher_dir.name / "virtiofsd-timeline-summary.txt").read_text()
    assert "post_acpi_samples=1" in summary
    assert "stop_reason=capture-teardown" in summary


def test_capture_selects_passive_timeline_without_invoking_qmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(host_qemu, "_observe", lambda *_args: calls.append("qmp"))
    monkeypatch.setattr(host_qemu, "_observe_virtiofsd_timeline", lambda *_args: calls.append("timeline"))

    with host_qemu.capture(lambda *_args, **_kwargs: "", "systest", tmp_path, passive_virtiofsd_timeline=True):
        pass

    assert calls == ["timeline"]


class _FakeK3s:
    def __init__(self, *, fail_list: bool = False, pod_uid: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") -> None:
        self.fail_list = fail_list
        self.pod_uid = pod_uid
        self.called = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def kubectl(self, *args: str, timeout: float, stdout_only: bool) -> str:
        self.calls.append((args, timeout))
        if args[2:4] == ("get", "pod"):
            return json.dumps(
                {
                    "metadata": {"name": args[4], "uid": self.pod_uid},
                    "spec": {"containers": [{"name": "compute", "image": "launcher:test"}]},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"name": "compute", "state": {"running": {}}}],
                    },
                }
            )
        if self.fail_list:
            self.called.set()
            raise RuntimeError("libvirt socket disappeared")
        if args[-2:] == ("list", "--name"):
            return "systest_vm\n"
        if "qemu-monitor-command" in args:
            self.called.set()
            self.release.wait(timeout=timeout)
            return json.dumps({"return": {"status": "running", "running": True}})
        raise AssertionError(f"unexpected virsh call: {args}")


def test_virtiofs_snapshot_reserves_existing_qemu_and_qmp_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    deadlines: dict[str, float] = {}
    monkeypatch.setattr(host_qemu, "_snapshot_pod_status", lambda *args: None)

    def virtiofs(output: io.StringIO, pod_uid: str, stop: threading.Event, deadline: float) -> None:
        deadlines["virtiofs"] = deadline

    def qemu(output: io.StringIO, pod_uid: str, stop: threading.Event, deadline: float) -> None:
        deadlines["qemu"] = deadline

    monkeypatch.setattr(host_qemu, "_snapshot_host_virtiofsd", virtiofs)
    monkeypatch.setattr(host_qemu, "_snapshot_host_qemu", qemu)
    k3s = _FakeK3s()
    k3s.release.set()
    output = io.StringIO()

    host_qemu._snapshot_qemu(
        k3s.kubectl, "systest", "virt-launcher-vm-pod", k3s.pod_uid, output, threading.Event(), include_pod_status=True
    )

    assert deadlines["virtiofs"] <= deadlines["qemu"] - host_qemu.VIRTIOFSD_QEMU_RESERVE_SECONDS
    assert "QEMU_RESULT query-status" in output.getvalue()
    assert any("qemu-monitor-command" in args for args, _ in k3s.calls)


def _shutdown_artifacts(root: Path) -> Path:
    launcher = root / "serial" / "systest_virt-launcher-vm-pod_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" / "vmi"
    launcher.mkdir(parents=True)
    (launcher / "serial.log").write_bytes(host_qemu.SHUTDOWN_REQUESTED + b"\n")
    return launcher.parents[1]


@pytest.mark.parametrize("fail_list", [False, True])
def test_probe_is_bounded_stoppable_and_does_not_replace_scenario_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_list: bool
) -> None:
    monkeypatch.setattr(host_qemu, "REQUEST_GRACE_SECONDS", 0)
    monkeypatch.setattr(host_qemu, "POLL_SECONDS", 0.001)
    monkeypatch.setattr(host_qemu, "_snapshot_host_qemu", lambda *args, **kwargs: None)
    artifacts = tmp_path / "artifacts"
    _shutdown_artifacts(artifacts)
    k3s = _FakeK3s(fail_list=fail_list)
    failure = ValueError("original scenario failure")

    def fail_after_probe() -> None:
        try:
            assert k3s.called.wait(timeout=2)
            raise failure
        finally:
            k3s.release.set()

    with (
        pytest.raises(ValueError, match="original scenario failure") as caught,
        host_qemu.capture(k3s.kubectl, "systest", artifacts),
    ):
        fail_after_probe()

    assert caught.value is failure
    assert all(timeout <= host_qemu.COMMAND_TIMEOUT_SECONDS for _, timeout in k3s.calls)
    samples = list((artifacts / "host-qemu").glob("*/shutdown-0-*.txt"))
    assert len(samples) == 1
    evidence = samples[0].read_text()
    pod_line = next(
        line.removeprefix("POD_STATUS ") for line in evidence.splitlines() if line.startswith("POD_STATUS ")
    )
    assert json.loads(pod_line)["metadata"] == {
        "name": "virt-launcher-vm-pod",
        "uid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "deletionTimestamp": None,
    }
    if fail_list:
        assert "VIRSH_DOMAIN_LIST_ERROR" in evidence
        assert "libvirt socket disappeared" in evidence
    else:
        monitor_calls = [args for args, _ in k3s.calls if "qemu-monitor-command" in args]
        assert monitor_calls
        observed_queries = set()
        for args in monitor_calls:
            query = json.loads(args[-1])
            if query["execute"] == "human-monitor-command":
                observed_queries.add(query["arguments"]["command-line"])
            else:
                observed_queries.add(query["execute"])
        allowed_queries = {command for command, _ in host_qemu.QEMU_PROBES}
        assert observed_queries <= allowed_queries
        assert not any(thread.name.startswith("vm-host-qemu") for thread in threading.enumerate())


def test_capture_stops_without_guest_shutdown_request(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    k3s = _FakeK3s()

    with host_qemu.capture(k3s.kubectl, "systest", artifacts):
        pass

    assert not k3s.calls


def test_active_turn_captures_two_predeadline_samples_for_its_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_qemu, "TURN_SAMPLE_AFTER_SECONDS", 0)
    monkeypatch.setattr(host_qemu, "TURN_SAMPLE_GAP_SECONDS", 0.01)
    monkeypatch.setattr(host_qemu, "POLL_SECONDS", 0.001)
    monkeypatch.setattr(host_qemu, "_snapshot_host_qemu", lambda *_args: None)
    for name in ("vm-2", "vm-3"):
        serial = tmp_path / "serial" / f"systest_virt-launcher-{name}-abc_uid-{name}" / "vmi" / "serial.log"
        serial.parent.mkdir(parents=True)
        serial.write_bytes(b"guest ready\n")
    k3s = _FakeK3s()
    k3s.release.set()

    with (
        host_qemu.capture(k3s.kubectl, "systest", tmp_path),
        host_qemu.observe_active_turn(tmp_path, "vm-2", "sandbox-uid-2"),
    ):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = list((tmp_path / "host-qemu").glob("*/turn-*.txt"))
            if len(current) == 2 and all("QEMU_RESULT info irq" in sample.read_text() for sample in current):
                break
            time.sleep(0.01)

    samples = sorted((tmp_path / "host-qemu").glob("*/turn-*.txt"))
    assert len(samples) == 2
    assert {sample.parent.name for sample in samples} == {"systest_virt-launcher-vm-2-abc_uid-vm-2"}
    assert all("QEMU_RESULT query-cpus-fast" in sample.read_text() for sample in samples)
    assert all("sandbox_uid=sandbox-uid-2" in sample.read_text() for sample in samples)
    assert not (tmp_path / "host-qemu" / "active-turns" / "vm-2").exists()


def test_one_launcher_read_failure_does_not_stop_other_launcher_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_qemu, "REQUEST_GRACE_SECONDS", 0)
    monkeypatch.setattr(host_qemu, "POLL_SECONDS", 0.001)
    monkeypatch.setattr(host_qemu, "_snapshot_host_qemu", lambda *args, **kwargs: None)
    artifacts = tmp_path / "artifacts"
    bad = artifacts / "serial" / "systest_virt-launcher-bad-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" / "vmi"
    good = artifacts / "serial" / "systest_virt-launcher-good_ffffffff-1111-2222-3333-444444444444" / "vmi"
    for launcher in (bad, good):
        launcher.mkdir(parents=True)
        (launcher / "serial.log").write_bytes(host_qemu.SHUTDOWN_REQUESTED + b"\n")

    read_markers = host_qemu._launcher_markers

    def fail_one(launcher: Path, stop: threading.Event | None = None) -> tuple[bool, bool]:
        if "-bad-" in launcher.name:
            raise PermissionError("one retained serial file is unreadable")
        return read_markers(launcher, stop)

    monkeypatch.setattr(host_qemu, "_launcher_markers", fail_one)
    k3s = _FakeK3s()
    failure = ValueError("original scenario failure")

    def fail_after_probe() -> None:
        try:
            assert k3s.called.wait(timeout=2)
            raise failure
        finally:
            k3s.release.set()

    with (
        pytest.raises(ValueError, match="original scenario failure") as caught,
        host_qemu.capture(k3s.kubectl, "systest", artifacts),
    ):
        fail_after_probe()

    assert caught.value is failure
    assert len(list((artifacts / "host-qemu" / good.parent.name).glob("shutdown-0-*.txt"))) == 1
    errors = (artifacts / "host-qemu" / bad.parent.name / "collector-errors.txt").read_text()
    assert "one retained serial file is unreadable" in errors


@pytest.mark.parametrize("fail_list", [False, True])
def test_report_captures_unrequested_guest_without_touching_completed_or_foreign_guests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_list: bool
) -> None:
    monkeypatch.setattr(host_qemu, "_snapshot_host_qemu", lambda *args, **kwargs: None)
    pending = tmp_path / "serial" / "systest_virt-launcher-pending_uid-pending"
    complete = tmp_path / "serial" / "systest_virt-launcher-complete_uid-complete"
    foreign = tmp_path / "serial" / "other_virt-launcher-pending_uid-foreign"
    for launcher, contents in (
        (pending, b"runtime started\n"),
        (complete, host_qemu.SHUTDOWN_COMPLETE + b"\n"),
        (foreign, b"runtime started\n"),
    ):
        serial = launcher / "vmi" / "serial.log"
        serial.parent.mkdir(parents=True)
        serial.write_bytes(contents)
    k3s = _FakeK3s(fail_list=fail_list)
    k3s.release.set()

    host_qemu.snapshot_pending(k3s.kubectl, "systest", tmp_path)

    files = list((tmp_path / "host-qemu").glob("*/*.txt"))
    assert len(files) == 1
    assert files[0].parent.name == pending.name
    evidence = files[0].read_text()
    assert "phase=report" in evidence
    assert host_qemu.SHUTDOWN_REQUESTED.decode() not in evidence
    assert k3s.calls
    assert all(args[3] == "virt-launcher-pending" for args, _ in k3s.calls)
    if fail_list:
        assert "libvirt socket disappeared" in evidence


def test_pod_snapshot_retains_only_matching_compute_and_virtiofs_lifecycle() -> None:
    pod = "virt-launcher-vm-pod"
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    payload = {
        "metadata": {
            "name": pod,
            "uid": uid,
            "creationTimestamp": "2026-11-06T10:00:00Z",
            "deletionTimestamp": "2026-11-06T10:01:00Z",
            "annotations": {"credential": "PRIVATE_ANNOTATION"},
        },
        "spec": {
            "nodeName": "worker-1",
            "terminationGracePeriodSeconds": 30,
            "containers": [
                {"name": "compute", "image": "launcher:test", "env": [{"value": "PRIVATE_ENV"}]},
                {
                    "name": "virtiofs-state",
                    "image": "virtiofs:test",
                    "resources": {"limits": {"memory": "512Mi", "private": "PRIVATE_RESOURCE"}},
                    "args": ["PRIVATE_ARG"],
                },
                {"name": "unrelated", "image": "PRIVATE_IMAGE"},
            ],
        },
        "status": {
            "phase": "Running",
            "startTime": "2026-11-06T10:00:05Z",
            "containerStatuses": [
                {
                    "name": "compute",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2026-11-06T10:00:10Z"}},
                },
                {
                    "name": "virtiofs-state",
                    "ready": False,
                    "restartCount": 1,
                    "state": {
                        "terminated": {
                            "exitCode": 137,
                            "reason": "OOMKilled",
                            "startedAt": "2026-11-06T10:00:11Z",
                            "finishedAt": "2026-11-06T10:00:55Z",
                            "message": "PRIVATE_MESSAGE",
                        }
                    },
                },
                {"name": "unrelated", "state": {"running": {"startedAt": "2026-11-06T10:00:10Z"}}},
            ],
        },
    }
    calls: list[tuple[tuple[str, ...], float]] = []

    def kubectl(*args: str, timeout: float, stdout_only: bool) -> str:
        calls.append((args, timeout))
        return json.dumps(payload)

    output = io.StringIO()
    host_qemu._snapshot_pod_status(kubectl, "systest", pod, uid, output, threading.Event(), time.monotonic() + 2)

    assert len(calls) == 1
    assert calls[0][0] == ("-n", "systest", "get", "pod", pod, "-o", "json")
    assert 0 < calls[0][1] <= host_qemu.COMMAND_TIMEOUT_SECONDS
    retained = output.getvalue()
    assert retained.startswith("POD_STATUS ")
    snapshot = json.loads(retained.removeprefix("POD_STATUS "))
    assert snapshot["metadata"]["uid"] == uid
    assert snapshot["metadata"]["deletionTimestamp"] == "2026-11-06T10:01:00Z"
    assert [container["name"] for container in snapshot["spec"]["containers"]] == ["compute", "virtiofs-state"]
    assert snapshot["spec"]["containers"][1]["resources"] == {"limits": {"memory": "512Mi"}}
    assert snapshot["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] == "2026-11-06T10:00:10Z"
    assert snapshot["status"]["containerStatuses"][1]["state"]["terminated"] == {
        "exitCode": 137,
        "reason": "OOMKilled",
        "startedAt": "2026-11-06T10:00:11Z",
        "finishedAt": "2026-11-06T10:00:55Z",
    }
    assert not any(
        private in retained
        for private in (
            "PRIVATE_ANNOTATION",
            "PRIVATE_ENV",
            "PRIVATE_ARG",
            "PRIVATE_IMAGE",
            "PRIVATE_RESOURCE",
            "PRIVATE_MESSAGE",
        )
    )


def test_pod_snapshot_retains_restartable_virtiofs_init_sidecar_lifecycle() -> None:
    pod = "virt-launcher-vm-pod"
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    payload = {
        "metadata": {"name": pod, "uid": uid, "deletionTimestamp": None},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{"name": "compute", "image": "quay.io/kubevirt/virt-launcher:v1.8.2"}],
            "initContainers": [
                {
                    "name": "virtiofs-state",
                    "image": "quay.io/kubevirt/virt-launcher:v1.8.2",
                    "restartPolicy": "Always",
                    "resources": {"requests": {"memory": "1M"}, "limits": {"memory": "80M"}},
                    "env": [{"value": "PRIVATE_INIT_ENV"}],
                    "args": ["PRIVATE_INIT_ARG"],
                },
                {"name": "unrelated-init", "image": "PRIVATE_INIT_IMAGE"},
            ],
        },
        "status": {
            "containerStatuses": [{"name": "compute", "ready": True, "state": {"running": {}}}],
            "initContainerStatuses": [
                {
                    "name": "virtiofs-state",
                    "ready": False,
                    "started": True,
                    "restartCount": 1,
                    "state": {
                        "terminated": {
                            "exitCode": 137,
                            "reason": "OOMKilled",
                            "startedAt": "2026-11-06T10:00:11Z",
                            "finishedAt": "2026-11-06T10:00:55Z",
                            "message": "PRIVATE_INIT_MESSAGE",
                        }
                    },
                    "lastState": {
                        "terminated": {"exitCode": 1, "reason": "Error", "finishedAt": "2026-11-06T09:59:59Z"}
                    },
                },
                {"name": "unrelated-init", "state": {"terminated": {"message": "PRIVATE_INIT_STATUS"}}},
            ],
        },
    }

    snapshot = host_qemu._pod_status_snapshot(payload, pod, uid)

    assert snapshot is not None
    spec = snapshot["spec"]
    status = snapshot["status"]
    assert isinstance(spec, dict)
    assert isinstance(status, dict)
    assert spec["containers"] == [{"name": "compute", "image": "quay.io/kubevirt/virt-launcher:v1.8.2"}]
    assert spec["initContainers"] == [
        {
            "name": "virtiofs-state",
            "image": "quay.io/kubevirt/virt-launcher:v1.8.2",
            "restartPolicy": "Always",
            "resources": {"requests": {"memory": "1M"}, "limits": {"memory": "80M"}},
        }
    ]
    assert status["initContainerStatuses"] == [
        {
            "name": "virtiofs-state",
            "ready": False,
            "started": True,
            "restartCount": 1,
            "state": {
                "terminated": {
                    "exitCode": 137,
                    "reason": "OOMKilled",
                    "startedAt": "2026-11-06T10:00:11Z",
                    "finishedAt": "2026-11-06T10:00:55Z",
                }
            },
            "lastState": {"terminated": {"exitCode": 1, "reason": "Error", "finishedAt": "2026-11-06T09:59:59Z"}},
        }
    ]
    retained = json.dumps(snapshot)
    assert all(
        private not in retained
        for private in (
            "PRIVATE_INIT_ENV",
            "PRIVATE_INIT_ARG",
            "PRIVATE_INIT_IMAGE",
            "PRIVATE_INIT_MESSAGE",
            "PRIVATE_INIT_STATUS",
        )
    )


def test_pod_snapshot_rejects_replaced_launcher_uid_without_leaking_status() -> None:
    pod = "virt-launcher-vm-pod"
    payload = {
        "metadata": {"name": pod, "uid": "replacement-uid"},
        "spec": {"containers": [{"name": "compute", "env": [{"value": "PRIVATE_ENV"}]}]},
        "status": {
            "containerStatuses": [{"name": "compute", "state": {"running": {"startedAt": "2026-11-06T10:00:10Z"}}}]
        },
    }

    def kubectl(*args: str, timeout: float, stdout_only: bool) -> str:
        return json.dumps(payload)

    output = io.StringIO()
    host_qemu._snapshot_pod_status(
        kubectl, "systest", pod, "original-uid", output, threading.Event(), time.monotonic() + 2
    )

    assert output.getvalue() == "POD_STATUS_UID_OR_SHAPE_MISMATCH\n"


if __name__ == "__main__":
    pytest_bazel.main()
