"""Guest log retention across launcher removal and container log replacement."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import partial
from pathlib import Path

import pytest
import pytest_bazel
from tenacity import Retrying, stop_after_delay, wait_fixed

from agentplane.system_tests.vm.guest_logs import (
    CONSOLES_ENV,
    SHUTDOWN_COMPLETE,
    SUPERVISION_FAILED,
    SUPERVISION_PASSED,
    GuestBootError,
    GuestBootEvidenceError,
    GuestShutdownError,
    capture,
    check_boot,
    systemd_supervision_passed,
)

MARKER = "2026-10-27T21:41:13.112914838Z stdout F " + SHUTDOWN_COMPLETE.decode() + "\n"
BOOT_EVIDENCE = """2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_BEGIN
2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_BEGIN /usr/bin/busybox cat /proc/cmdline
2026-10-27T21:41:13Z stdout F root=/dev/ram console=ttyS0 agentplane.boot-evidence=1
2026-10-27T21:41:13Z stdout F AGENTPLANE_UDEV_MONITOR_READY
2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_GUEST_END
2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_DEVICE_END
2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_ACTIVATION_END
2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_END status=complete
"""

CaptureLogs = Callable[..., AbstractContextManager[None]]


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    return tmp_path / "pods"


@pytest.fixture
def kubelet_pods(tmp_path: Path) -> Path:
    return tmp_path / "kubelet-pods"


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def outputs(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def capture_logs(logs: Path, kubelet_pods: Path, storage: Path, outputs: Path) -> CaptureLogs:
    """Leaves `validate_shutdown` and the `require_*` options to each test."""
    return partial(
        capture,
        log_root=logs,
        pod_root=kubelet_pods,
        storage_root=storage,
        namespace="systest",
        kubevirt_namespace="kubevirt",
        destination=outputs,
    )


def _eventually() -> Retrying:
    return Retrying(stop=stop_after_delay(5), wait=wait_fixed(0.01), reraise=True)


def test_boot_failure_requires_a_complete_console_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONSOLES_ENV, str(tmp_path))
    console = tmp_path / "test-launcher" / "console.cri.log"
    console.parent.mkdir()
    marker = b"2026-10-27T21:41:13Z stdout F " + SUPERVISION_FAILED
    console.write_bytes(marker.replace(b"stdout F ", b"stdout F + echo ") + b"\n" + marker)
    check_boot()
    with console.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(GuestBootError, match="test-launcher"):
        check_boot()


def test_proof_success_requires_a_complete_console_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONSOLES_ENV, str(tmp_path))
    assert not systemd_supervision_passed("selected")
    console = tmp_path / "test-launcher_selected" / "console.cri.log"
    console.parent.mkdir()
    marker = b"2026-10-28T21:41:13Z stdout F " + SUPERVISION_PASSED
    console.write_bytes(marker.replace(b"stdout F ", b"stdout F + printf ") + b"\n" + marker)
    assert not systemd_supervision_passed("selected")
    with console.open("ab") as stream:
        stream.write(b"\n")
    assert systemd_supervision_passed("selected")
    console.write_bytes(
        b"2026-10-28T21:41:13Z stdout F " + SUPERVISION_PASSED + b"[   13.327261] systemd[1]: Starting service\n"
    )
    assert not systemd_supervision_passed("selected")
    assert not systemd_supervision_passed("another-vm")


def test_retains_unlinked_logs_and_new_incarnations(logs: Path, outputs: Path, capture_logs: CaptureLogs) -> None:
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    other = logs / "other_virt-launcher-test_uid-two" / "guest-console-log" / "0.log"
    other.parent.mkdir(parents=True)
    other.write_text("OUTSIDE_NAMESPACE\n")

    def wait_for(text: str) -> None:
        for attempt in _eventually():
            with attempt:
                assert text in "".join(path.read_text() for path in outputs.rglob("*.log"))

    with capture_logs(validate_shutdown=lambda: True), source.open("w") as original:
        original.write("BOOT\n")
        original.flush()
        wait_for("BOOT")
        source.unlink()
        original.write("AFTER_UNLINK\n")
        original.flush()
        wait_for("AFTER_UNLINK")
        source.write_text("NEW_CONTAINER\n")
        wait_for("NEW_CONTAINER")
        replacement = logs / "systest_virt-launcher-test_uid-three" / "guest-console-log" / "0.log"
        replacement.parent.mkdir(parents=True)
        replacement.write_text("NEW_LAUNCHER\n" + MARKER)
        wait_for("NEW_LAUNCHER")
        # Do not wait for another poll: capture's final drain must collect this from the unlinked fd.
        original.write(MARKER)
        original.flush()

    assert sorted(path.read_text() for path in outputs.rglob("*.log")) == [
        "BOOT\nAFTER_UNLINK\n" + MARKER,
        "NEW_CONTAINER\n",
        "NEW_LAUNCHER\n" + MARKER,
    ]


def test_requires_shutdown_for_each_launcher(logs: Path, capture_logs: CaptureLogs) -> None:
    traced_echo = MARKER.replace("stdout F ", "stdout F + echo ")
    interleaved_printk = MARKER.rstrip("\n") + "[   13.327261] systemd[1]: Starting service\n"
    for uid, content in [("one", MARKER), ("two", traced_echo), ("three", interleaved_printk)]:
        source = logs / f"systest_virt-launcher-test_uid-{uid}" / "guest-console-log" / "0.log"
        source.parent.mkdir(parents=True)
        source.write_text(content)

    with (
        pytest.raises(GuestShutdownError, match="systest_virt-launcher-test_uid-two"),
        capture_logs(validate_shutdown=lambda: True),
    ):
        pass


@pytest.mark.parametrize("raw_serial", [False, True])
def test_retains_shutdown_sources_after_launcher_removal(
    logs: Path, kubelet_pods: Path, outputs: Path, capture_logs: CaptureLogs, raw_serial: bool
) -> None:
    launcher = logs / "systest_virt-launcher-test_uid-one"
    launcher.mkdir(parents=True)
    if raw_serial:
        relative = "volumes/kubernetes.io~empty-dir/private/vmi-one/virt-serial0-log"
        source = kubelet_pods / "uid-one" / relative
        foreign = kubelet_pods / "uid-other" / relative
        foreign.parent.mkdir(parents=True)
        foreign.write_text("FOREIGN_SERIAL\n")
        retained = outputs / "serial"
    else:
        source = launcher / "virtiofs-state" / "0.log"
        retained = outputs / "virtiofs"
    source.parent.mkdir(parents=True)
    with capture_logs(validate_shutdown=lambda: False), source.open("w") as original:
        original.write("FUSE_REQUEST\n")
        original.flush()
        for attempt in _eventually():
            with attempt:
                assert [p.read_text() for p in retained.rglob("*.log")] == ["FUSE_REQUEST\n"]
        source.unlink()
        original.write("FUSE_SHUTDOWN\n")
        original.flush()

    assert [p.read_text() for p in retained.rglob("*.log")] == ["FUSE_REQUEST\nFUSE_SHUTDOWN\n"]


def test_retains_native_journal_header_changes_after_unlink_on_failure(
    storage: Path, outputs: Path, capture_logs: CaptureLogs
) -> None:
    source = storage / "pvc-one_systest_vm-state/systemd-journal/machine/system.journal"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"initial header\x00entries")
    foreign = storage / "pvc-two_other_vm-state/systemd-journal/machine/system.journal"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"FOREIGN_JOURNAL")

    def fail_scenario() -> None:
        with capture_logs(validate_shutdown=lambda: True), source.open("r+b") as journal:
            for attempt in _eventually():
                with attempt:
                    assert [path.read_bytes() for path in outputs.rglob("*.journal")] == [b"initial header\x00entries"]
            source.unlink()
            journal.seek(0)
            journal.write(b"updated header\x00entries")
            journal.flush()
            for attempt in _eventually():
                with attempt:
                    assert [path.read_bytes() for path in outputs.rglob("*.journal")] == [b"updated header\x00entries"]
            journal.seek(0)
            journal.write(b"updated header\x00entries\x00shutdown")
            journal.flush()
            raise ValueError("scenario failed")

    with pytest.raises(ValueError, match="scenario failed"):
        fail_scenario()

    assert [path.read_bytes() for path in outputs.rglob("*.journal")] == [b"updated header\x00entries\x00shutdown"]


def test_retains_unlinked_firecracker_console_and_only_namespace_logs(
    storage: Path, logs: Path, outputs: Path, capture_logs: CaptureLogs
) -> None:
    console = storage / "pvc-uid_systest_sandbox-state/firecracker/firecracker.log"
    console.parent.mkdir(parents=True)
    console.write_text("VMM_START\n")
    foreign_console = storage / "pvc-uid_other_sandbox-state/firecracker/firecracker.log"
    foreign_console.parent.mkdir(parents=True)
    foreign_console.write_text("FOREIGN_VMM\n")

    runtime_log = logs / "systest_environment-runtime-abc_uid-one/environment-runtime/0.log"
    runtime_log.parent.mkdir(parents=True)
    runtime_log.write_text("CRI_RUNTIME\n")
    foreign_runtime_log = logs / "other_environment-runtime-abc_uid-two/environment-runtime/0.log"
    foreign_runtime_log.parent.mkdir(parents=True)
    foreign_runtime_log.write_text("FOREIGN_RUNTIME\n")

    def wait_for(text: str) -> None:
        for attempt in _eventually():
            with attempt:
                assert text in "".join(path.read_text() for path in outputs.rglob("*.log*"))

    with capture_logs(validate_shutdown=lambda: True, require_shutdown_marker=False), console.open("a") as original:
        wait_for("VMM_START")
        wait_for("CRI_RUNTIME")
        console.unlink()
        original.write("VMM_AFTER_UNLINK\n")
        original.flush()
        wait_for("VMM_AFTER_UNLINK")

    retained_console = list((outputs / "firecracker").rglob("*-firecracker.log.log"))
    assert len(retained_console) == 1
    assert retained_console[0].read_text() == "VMM_START\nVMM_AFTER_UNLINK\n"
    assert [path.read_text() for path in (outputs / "runtimes").rglob("*.cri.log")] == ["CRI_RUNTIME\n"]
    all_logs = "".join(path.read_text() for path in outputs.rglob("*.log*"))
    assert "FOREIGN_VMM" not in all_logs
    assert "FOREIGN_RUNTIME" not in all_logs


def test_retains_failure_evidence_without_masking_exception(
    logs: Path, outputs: Path, capture_logs: CaptureLogs
) -> None:
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text("FAILED_GUEST\n")
    failure = ValueError("original scenario failure")
    with (
        pytest.raises(ValueError, match="original scenario failure") as caught,
        capture_logs(validate_shutdown=lambda: True, require_boot_evidence=True),
    ):
        raise failure
    assert caught.value is failure
    assert [path.read_text() for path in outputs.rglob("*.log")] == ["FAILED_GUEST\n"]


def test_session_failures_disable_validation_at_teardown(logs: Path, outputs: Path, capture_logs: CaptureLogs) -> None:
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text("FAILED_GUEST\n")
    failures = 0
    with capture_logs(validate_shutdown=lambda: failures == 0, require_boot_evidence=True):
        # pytest reports failed cases without throwing their exception into the session fixture.
        failures += 1
    assert [path.read_text() for path in outputs.rglob("*.log")] == ["FAILED_GUEST\n"]


def test_boot_readiness_accepts_interleaved_kernel_printk(logs: Path, capture_logs: CaptureLogs) -> None:
    source = logs / "systest_virt-launcher-test_uid-oom" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    evidence = BOOT_EVIDENCE.replace(
        "2026-10-27T21:41:13Z stdout F AGENTPLANE_UDEV_MONITOR_READY\n",
        "2026-10-27T21:41:13Z stdout F AGENTPLANE_UDEV_MONITOR_READY"
        "[   13.327261] systemd[1]: Starting systemd-remount-fs.service...\n",
    )
    source.write_text(evidence + MARKER)

    with capture_logs(validate_shutdown=lambda: True, require_boot_evidence=True):
        pass


@pytest.mark.parametrize(
    ("cri_fields", "payload", "terminated"),
    [
        ("stdout F", "AGENTPLANE_UDEV_MONITOR_READY_NOT_A_RECEIPT", True),
        ("stdout F", "AGENTPLANE_UDEV_MONITOR_READY arbitrary suffix", True),
        ("stdout F", "AGENTPLANE_UDEV_MONITOR_READY[   13.327261]", True),
        ("stdout F", "AGENTPLANE_UDEV_MONITOR_READY[   13.32726] systemd[1]", True),
        ("stdout F", "AGENTPLANE_UDEV_MONITOR_READY[   x.327261] systemd[1]", True),
        ("stdout P", "AGENTPLANE_UDEV_MONITOR_READY[   13.327261] systemd[1]", True),
        ("stderr F", "AGENTPLANE_UDEV_MONITOR_READY[   13.327261] systemd[1]", True),
        ("stdout F", "+ echo AGENTPLANE_UDEV_MONITOR_READY[   13.327261] systemd[1]", True),
        ("stdout F", "AGENTPLANE_UDEV_MONITOR_READY[   13.327261] systemd[1]", False),
    ],
)
def test_boot_readiness_rejects_non_receipts_and_malformed_printk_frames(
    logs: Path, capture_logs: CaptureLogs, cri_fields: str, payload: str, terminated: bool
) -> None:
    source = logs / "systest_virt-launcher-test_uid-oom" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    normal_ready = "2026-10-27T21:41:13Z stdout F AGENTPLANE_UDEV_MONITOR_READY\n"
    readiness_record = f"2026-10-27T21:41:13Z {cri_fields} {payload}"
    if terminated:
        evidence = BOOT_EVIDENCE.replace(normal_ready, readiness_record + "\n")
        source.write_text(evidence + MARKER)
    else:
        # Keep the truncated record separate from the complete boot evidence and shutdown receipt.
        source.write_text(BOOT_EVIDENCE.replace(normal_ready, "") + MARKER)
        (source.parent / "truncated.cri.log").write_bytes(readiness_record.encode())

    with (
        pytest.raises(GuestBootEvidenceError, match="did not report readiness"),
        capture_logs(validate_shutdown=lambda: True, require_boot_evidence=True),
    ):
        pass


def test_successful_teardown_requires_complete_evidence_from_selected_guest(
    logs: Path, capture_logs: CaptureLogs
) -> None:
    selected = logs / "systest_virt-launcher-test_uid-oom" / "guest-console-log" / "0.log"
    selected.parent.mkdir(parents=True)
    selected.write_text(BOOT_EVIDENCE + MARKER)
    other = logs / "systest_virt-launcher-test_uid-runner" / "guest-console-log" / "0.log"
    other.parent.mkdir(parents=True)
    other.write_text(
        "2026-10-27T21:41:13Z stdout F [   10.883319] systemd[1]: agentplane-boot-device-monitor.service "
        "was skipped because of an unmet condition check "
        "(ConditionKernelCommandLine=agentplane.boot-evidence=1).\n" + MARKER
    )

    with capture_logs(validate_shutdown=lambda: True, require_boot_evidence=True):
        pass


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ("", "Expected exactly one retained guest"),
        (BOOT_EVIDENCE.replace("AGENTPLANE_UDEV_MONITOR_READY\n", ""), "did not report readiness"),
        (
            BOOT_EVIDENCE.replace(
                "AGENTPLANE_BOOT_EVIDENCE_END status=complete\n",
                "AGENTPLANE_BOOT_EVIDENCE_END status=partial\n"
                "2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_REQUIRED_FAILURES 1\n",
            ),
            "was partial",
        ),
        (BOOT_EVIDENCE.replace("AGENTPLANE_BOOT_EVIDENCE_ACTIVATION_END\n", ""), "missed a required section"),
        (
            BOOT_EVIDENCE.replace("2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_END status=complete\n", ""),
            "did not complete",
        ),
    ],
)
def test_successful_teardown_reports_incomplete_boot_evidence(
    logs: Path, outputs: Path, capture_logs: CaptureLogs, evidence: str, message: str
) -> None:
    source = logs / "systest_virt-launcher-test_uid-oom" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text(evidence + MARKER)

    with (
        pytest.raises(GuestBootEvidenceError, match=message),
        capture_logs(validate_shutdown=lambda: True, require_boot_evidence=True),
    ):
        pass
    assert evidence == "" or list(outputs.rglob("*.log"))


def test_successful_teardown_collects_evidence_split_across_rotated_guest_logs(
    logs: Path, capture_logs: CaptureLogs
) -> None:
    console = logs / "systest_virt-launcher-test_uid-oom" / "guest-console-log"
    console.mkdir(parents=True)
    (console / "0.log").write_text(
        "2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_BEGIN\n"
        "2026-10-27T21:41:13Z stdout F root=/dev/ram console=ttyS0 agentplane.boot-evidence=1\n"
        "2026-10-27T21:41:13Z stdout F AGENTPLANE_UDEV_MONITOR_READY\n"
    )
    (console / "1.log").write_text(
        "\n".join(
            "2026-10-27T21:41:13Z stdout F " + marker
            for marker in (
                "AGENTPLANE_BOOT_EVIDENCE_GUEST_END",
                "AGENTPLANE_BOOT_EVIDENCE_DEVICE_END",
                "AGENTPLANE_BOOT_EVIDENCE_ACTIVATION_END",
                "AGENTPLANE_BOOT_EVIDENCE_END status=complete",
                SHUTDOWN_COMPLETE.decode(),
            )
        )
        + "\n"
    )

    with capture_logs(validate_shutdown=lambda: True, require_boot_evidence=True):
        pass


def test_retains_only_scoped_console_compute_relay_and_handler_logs(
    logs: Path, outputs: Path, capture_logs: CaptureLogs
) -> None:
    contents = {
        "systest_virt-launcher-test_uid-one/guest-console-log/0.log": MARKER,
        "systest_virt-launcher-test_uid-one/compute/0.log": "COMPUTE\n",
        "kubevirt_virt-handler-test_uid-two/virt-handler/0.log": "HANDLER\n",
        "other_virt-launcher-test_uid-three/compute/0.log": "FOREIGN_LAUNCHER\n",
        "other_virt-handler-test_uid-four/virt-handler/0.log": "FOREIGN_HANDLER\n",
        "systest_virt-launcher-test_uid-one/egress-sidecar/0.log": "RELAY\n",
        "other_virt-launcher-test_uid-three/egress-sidecar/0.log": "FOREIGN_RELAY\n",
        "kubevirt_virt-controller-test_uid-five/virt-controller/0.log": "CONTROLLER\n",
    }
    for relative, content in contents.items():
        path = logs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    with capture_logs(validate_shutdown=lambda: True):
        pass
    assert sorted(path.read_text() for path in outputs.rglob("*.log")) == sorted(
        [MARKER, "COMPUTE\n", "HANDLER\n", "RELAY\n"]
    )
    assert {path.parts[0] for path in (path.relative_to(outputs) for path in outputs.rglob("*.log"))} == {
        "consoles",
        "launchers",
        "relays",
        "handlers",
    }


if __name__ == "__main__":
    pytest_bazel.main()
