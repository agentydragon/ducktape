"""Guest log retention across launcher removal and container log replacement."""

from pathlib import Path

import pytest
import pytest_bazel
from tenacity import Retrying, stop_after_delay, wait_fixed

from agentplane.system_tests.k3s.guest_logs import (
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
2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_JOURNAL_END
2026-10-27T21:41:13Z stdout F AGENTPLANE_BOOT_EVIDENCE_END status=complete
"""


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
    assert not systemd_supervision_passed("another-vm")


def test_retains_unlinked_logs_and_new_incarnations(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    other = logs / "other_virt-launcher-test_uid-two" / "guest-console-log" / "0.log"
    other.parent.mkdir(parents=True)
    other.write_text("OUTSIDE_NAMESPACE\n")

    def wait_for(text: str) -> None:
        for attempt in Retrying(stop=stop_after_delay(5), wait=wait_fixed(0.01), reraise=True):
            with attempt:
                assert text in "".join(path.read_text() for path in outputs.rglob("*.log"))

    with (
        capture(
            log_root=logs,
            namespace="systest",
            kubevirt_namespace="kubevirt",
            destination=outputs,
            validate_shutdown=lambda: True,
        ),
        source.open("w") as original,
    ):
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


def test_requires_shutdown_for_each_launcher(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    traced_echo = MARKER.replace("stdout F ", "stdout F + echo ")
    for uid, content in [("one", MARKER), ("two", traced_echo)]:
        source = logs / f"systest_virt-launcher-test_uid-{uid}" / "guest-console-log" / "0.log"
        source.parent.mkdir(parents=True)
        source.write_text(content)

    with (
        pytest.raises(GuestShutdownError, match="systest_virt-launcher-test_uid-two"),
        capture(
            log_root=logs,
            namespace="systest",
            kubevirt_namespace="kubevirt",
            destination=outputs,
            validate_shutdown=lambda: True,
        ),
    ):
        pass


def test_retains_failure_evidence_without_masking_exception(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text("FAILED_GUEST\n")
    failure = ValueError("original scenario failure")
    with (
        pytest.raises(ValueError, match="original scenario failure") as caught,
        capture(
            log_root=logs,
            namespace="systest",
            kubevirt_namespace="kubevirt",
            destination=outputs,
            validate_shutdown=lambda: True,
            require_boot_evidence=True,
        ),
    ):
        raise failure
    assert caught.value is failure
    assert [path.read_text() for path in outputs.rglob("*.log")] == ["FAILED_GUEST\n"]


def test_session_failures_disable_validation_at_teardown(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text("FAILED_GUEST\n")
    failures = 0
    with capture(
        log_root=logs,
        namespace="systest",
        kubevirt_namespace="kubevirt",
        destination=outputs,
        validate_shutdown=lambda: failures == 0,
        require_boot_evidence=True,
    ):
        # pytest reports failed cases without throwing their exception into the session fixture.
        failures += 1
    assert [path.read_text() for path in outputs.rglob("*.log")] == ["FAILED_GUEST\n"]


def test_successful_teardown_requires_complete_evidence_from_selected_guest(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
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

    with capture(
        log_root=logs,
        namespace="systest",
        kubevirt_namespace="kubevirt",
        destination=outputs,
        validate_shutdown=lambda: True,
        require_boot_evidence=True,
    ):
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
def test_successful_teardown_reports_incomplete_boot_evidence(tmp_path: Path, evidence: str, message: str) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    source = logs / "systest_virt-launcher-test_uid-oom" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text(evidence + MARKER)

    with (
        pytest.raises(GuestBootEvidenceError, match=message),
        capture(
            log_root=logs,
            namespace="systest",
            kubevirt_namespace="kubevirt",
            destination=outputs,
            validate_shutdown=lambda: True,
            require_boot_evidence=True,
        ),
    ):
        pass
    assert evidence == "" or list(outputs.rglob("*.log"))


def test_successful_teardown_collects_evidence_split_across_rotated_guest_logs(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
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
                "AGENTPLANE_BOOT_EVIDENCE_JOURNAL_END",
                "AGENTPLANE_BOOT_EVIDENCE_END status=complete",
                SHUTDOWN_COMPLETE.decode(),
            )
        )
        + "\n"
    )

    with capture(
        log_root=logs,
        namespace="systest",
        kubevirt_namespace="kubevirt",
        destination=outputs,
        validate_shutdown=lambda: True,
        require_boot_evidence=True,
    ):
        pass


def test_retains_only_scoped_console_compute_relay_and_handler_logs(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
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
    with capture(
        log_root=logs,
        namespace="systest",
        kubevirt_namespace="kubevirt",
        destination=outputs,
        validate_shutdown=lambda: True,
    ):
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
