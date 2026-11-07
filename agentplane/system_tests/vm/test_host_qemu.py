"""Host-side shutdown diagnostics stay scoped and cannot fail the VM scenario."""

import io
import json
import threading
import time
from pathlib import Path

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
