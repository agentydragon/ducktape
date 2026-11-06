"""Guest-only VM shutdown evidence classification."""

import json
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.vm.shutdown_diagnostic import record, require_orderly_shutdown, write_progress

NAMESPACE = "systest"
VMI_UID = "fake-vmi-uid"


def _serial(artifacts: Path, content: bytes, *, uid: str = VMI_UID) -> Path:
    path = artifacts / f"serial/{NAMESPACE}_virt-launcher-probe_uid-one/{uid}/virt-serial0-log.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def _cri(component: str, message: str, *, uid: str = VMI_UID) -> str:
    entry = json.dumps({"component": component, "msg": message, "uid": uid})
    return f"2026-11-06T00:00:00Z stderr F {entry}\n"


def _host_logs(artifacts: Path, *, forced: bool = False, shutdown_event: bool = True, uid: str = VMI_UID) -> None:
    handler = artifacts / "handlers/handler.log"
    launcher = artifacts / "launchers/compute.log"
    handler.parent.mkdir(parents=True, exist_ok=True)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if forced:
        handler.write_text(
            _cri("virt-handler", "Grace period expired, killing deleted VirtualMachineInstance test-vm", uid=uid)
        )
        launcher.write_text(
            _cri("virt-launcher", "Signaled vmi kill", uid=uid)
            + _cri(
                "virt-launcher",
                'DomainLifecycle event Domain event="stopped" detail="destroyed" with event id 5 reason 1 received',
                uid=uid,
            )
        )
    else:
        # These are actual KubeVirt messages observed in retained successful guest teardown logs.
        handler_message = "Domain is in state Shutoff reason Shutdown" if shutdown_event else "Observed VMI update"
        handler.write_text(_cri("virt-handler", handler_message, uid=uid))
        launcher.write_text(_cri("virt-launcher", "Domain stopped.", uid=uid))


def test_records_ordered_guest_shutdown_and_correlated_host_evidence(tmp_path: Path) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    _serial(
        artifacts,
        b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE\n[  72.512345] reboot: Power down\n",
    )
    _host_logs(artifacts)
    # A prior VMI with a reused name expired its grace period. UID correlation must exclude it.
    with (artifacts / "handlers/handler.log").open("a") as output_file:
        output_file.write(
            _cri("virt-handler", "Grace period expired, killing deleted VirtualMachineInstance old", uid="old-uid")
        )
    write_progress(output, "sandbox_deleted", sandbox_uid="fake-sandbox-uid")

    report = record(artifacts, output, NAMESPACE)

    assert report["classification"] == "orderly_guest_powerdown"
    guest = report["guests"][0]
    assert guest["vmi_uid"] == VMI_UID
    assert guest["acpi_request_lines"] == ["AGENTPLANE_GUEST_SHUTDOWN_REQUESTED"]
    assert guest["shutdown_complete_lines"] == ["AGENTPLANE_GUEST_SHUTDOWN_COMPLETE"]
    assert guest["domain_powerdown_lines"] == ["[  72.512345] reboot: Power down"]
    assert guest["ordered_shutdown_proof"] is True
    lifecycle = guest["host_lifecycle"]
    assert lifecycle is not None
    assert lifecycle["observation"] == "correlated_host_logs_present"
    assert lifecycle["events"]["grace_period_expired"] == []
    assert report["handler_artifacts"] == ["handler.log"]
    require_orderly_shutdown(report)


def test_correlates_and_classifies_a_kubevirt_forced_stop(tmp_path: Path) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    _serial(
        artifacts,
        b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE\n[  72.512345] reboot: Power down\n",
    )
    _host_logs(artifacts, forced=True)
    write_progress(output, "sandbox_deleted")

    report = record(artifacts, output, NAMESPACE)
    lifecycle = report["guests"][0]["host_lifecycle"]

    assert report["classification"] == "guest_powerdown_with_host_forced_stop"
    assert lifecycle is not None
    assert len(lifecycle["events"]["grace_period_expired"]) == 1
    assert len(lifecycle["events"]["virt_launcher_kill"]) == 1
    assert len(lifecycle["events"]["domain_stopped_destroyed"]) == 1
    with pytest.raises(RuntimeError, match="guest_powerdown_with_host_forced_stop"):
        require_orderly_shutdown(report)


def test_domain_stopped_destroyed_is_a_forced_stop_without_kill_log(tmp_path: Path) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    _serial(
        artifacts,
        b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE\n[  72.512345] reboot: Power down\n",
    )
    handler = artifacts / "handlers/handler.log"
    launcher = artifacts / "launchers/compute.log"
    handler.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    handler.write_text(_cri("virt-handler", "Observed VMI update"))
    launcher.write_text(
        _cri(
            "virt-launcher",
            'DomainLifecycle event Domain event="stopped" detail="destroyed" with event id 5 reason 1 received',
        )
    )
    write_progress(output, "sandbox_deleted")

    report = record(artifacts, output, NAMESPACE)

    assert report["classification"] == "guest_powerdown_with_host_forced_stop"
    lifecycle = report["guests"][0]["host_lifecycle"]
    assert lifecycle is not None
    events = lifecycle["events"]
    assert events["domain_stopped_destroyed"]
    assert events["grace_period_expired"] == []
    assert events["virt_launcher_kill"] == []


def test_missing_correlated_host_logs_is_not_reported_as_no_forced_stop(tmp_path: Path) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    _serial(
        artifacts,
        b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE\n[  72.512345] reboot: Power down\n",
    )
    _host_logs(artifacts, uid="unrelated-vmi")
    write_progress(output, "sandbox_deleted")

    report = record(artifacts, output, NAMESPACE)

    assert report["classification"] == "guest_powerdown_host_lifecycle_logs_missing"
    lifecycle = report["guests"][0]["host_lifecycle"]
    assert lifecycle is not None
    assert lifecycle["observation"] == "correlated_handler_or_launcher_logs_missing"


def test_correlated_host_logs_without_shutdown_event_remain_unknown(tmp_path: Path) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    _serial(
        artifacts,
        b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE\n[  72.512345] reboot: Power down\n",
    )
    _host_logs(artifacts, shutdown_event=False)
    write_progress(output, "sandbox_deleted")

    report = record(artifacts, output, NAMESPACE)

    assert report["classification"] == "guest_powerdown_host_shutdown_event_missing"
    lifecycle = report["guests"][0]["host_lifecycle"]
    assert lifecycle is not None
    assert lifecycle["observation"] == "correlated_host_logs_present"
    assert lifecycle["events"]["domain_shutdown"] == []
    assert lifecycle["events"]["domain_shutoff_shutdown"] == []
    with pytest.raises(RuntimeError, match="guest_powerdown_host_shutdown_event_missing"):
        require_orderly_shutdown(report)


@pytest.mark.parametrize(
    ("phase", "serial", "classification"),
    [
        ("not_started", b"", "fixture_setup_failed_before_test"),
        ("started", b"", "sandbox_creation_or_guest_launch_not_observed_before_ready"),
        ("sandbox_deleted", b"boot complete\n", "guest_ready_acpi_receipt_missing"),
        ("sandbox_deleted", b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\n", "acpi_received_shutdown_proof_missing"),
        (
            "sandbox_deleted",
            b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE\n",
            "shutdown_markers_missing_or_out_of_order",
        ),
        (
            "sandbox_deleted",
            b"AGENTPLANE_GUEST_SHUTDOWN_COMPLETE\nAGENTPLANE_GUEST_SHUTDOWN_REQUESTED\n"
            b"[  72.512345] reboot: Power down\n",
            "shutdown_markers_missing_or_out_of_order",
        ),
        (
            "guest_ready",
            b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE\n"
            b"[  72.512345] reboot: Power down\n",
            "sandbox_deletion_not_completed",
        ),
    ],
)
def test_classifies_incomplete_lifecycle_or_shutdown_evidence(
    tmp_path: Path, phase: str, serial: bytes, classification: str
) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    if phase not in ("not_started", "started"):
        _serial(artifacts, serial)
    if phase not in ("not_started", "started"):
        _host_logs(artifacts)
    write_progress(output, phase)

    report = record(artifacts, output, NAMESPACE)

    assert report["classification"] == classification
    with pytest.raises(RuntimeError, match=classification):
        require_orderly_shutdown(report)


def test_reports_guest_launch_evidence_separately_from_fixture_setup(tmp_path: Path) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    console = artifacts / f"consoles/{NAMESPACE}_virt-launcher-probe_uid-one/console.log"
    console.parent.mkdir(parents=True)
    console.write_text("guest kernel boot started\n")
    write_progress(output, "started")

    report = record(artifacts, output, NAMESPACE)

    assert report["classification"] == "guest_launch_seen_before_ready"


def test_refuses_receipts_that_are_not_complete_lines(tmp_path: Path) -> None:
    output = tmp_path / "test.outputs"
    artifacts = output / "vm-artifacts"
    _serial(
        artifacts,
        b"not AGENTPLANE_GUEST_SHUTDOWN_REQUESTED\nAGENTPLANE_GUEST_SHUTDOWN_COMPLETE extra\nreboot: Power down",
    )
    write_progress(output, "sandbox_deleted")

    report = record(artifacts, output, NAMESPACE)

    assert report["classification"] == "guest_ready_acpi_receipt_missing"
    guest = report["guests"][0]
    assert guest["acpi_request_lines"] == []
    assert guest["shutdown_complete_lines"] == []
    assert guest["domain_powerdown_lines"] == []


if __name__ == "__main__":
    pytest_bazel.main()
