"""Record lifecycle, guest shutdown and KubeVirt domain evidence for one VM."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

ACPI_REQUESTED = b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED"
SHUTDOWN_COMPLETE = b"AGENTPLANE_GUEST_SHUTDOWN_COMPLETE"
DOMAIN_POWERDOWN = b"reboot: Power down"
PROGRESS_FILENAME = "vm-shutdown-diagnostic-progress.json"
REPORT_FILENAME = "shutdown-diagnostic.json"

DomainEventName = Literal[
    "grace_period_expired",
    "virt_launcher_kill",
    "domain_stopped_destroyed",
    "domain_shutdown",
    "domain_shutoff_shutdown",
]
HostLogObservation = Literal["correlated_host_logs_present", "correlated_handler_or_launcher_logs_missing"]
HostLogRoot = Literal["handlers", "launchers"]
ShutdownClassification = Literal[
    "fixture_setup_failed_before_test",
    "guest_launch_seen_before_ready",
    "sandbox_creation_or_guest_launch_not_observed_before_ready",
    "sandbox_deletion_not_completed",
    "unexpected_guest_count",
    "guest_ready_acpi_receipt_missing",
    "acpi_received_shutdown_proof_missing",
    "shutdown_markers_missing_or_out_of_order",
    "guest_powerdown_with_host_forced_stop",
    "guest_powerdown_host_lifecycle_logs_missing",
    "guest_powerdown_host_shutdown_event_missing",
    "orderly_guest_powerdown",
]


class ShutdownProgress(TypedDict):
    phase: str
    sandbox_uid: NotRequired[str]


class DomainEvents(TypedDict):
    grace_period_expired: list[str]
    virt_launcher_kill: list[str]
    domain_stopped_destroyed: list[str]
    domain_shutdown: list[str]
    domain_shutoff_shutdown: list[str]


class CorrelatedHostEntries(TypedDict):
    handlers: int
    launchers: int


class HostLifecycle(TypedDict):
    observation: HostLogObservation
    correlated_entries: CorrelatedHostEntries
    events: DomainEvents


class GuestReport(TypedDict):
    launcher: str
    vmi_uid: str | None
    serial_artifacts: list[str]
    acpi_request_lines: list[str]
    shutdown_complete_lines: list[str]
    domain_powerdown_lines: list[str]
    ordered_shutdown_proof: bool
    host_lifecycle: HostLifecycle | None


class ShutdownDiagnosticReport(TypedDict):
    classification: ShutdownClassification
    test_identity: dict[str, str]
    lifecycle: ShutdownProgress
    expected_guest_count: int
    guest_count: int
    guests: list[GuestReport]
    launcher_artifacts: list[str]
    handler_artifacts: list[str]
    virtiofs_artifacts: list[str]
    host_qemu_artifacts: list[str]


def write_progress(output_dir: Path, phase: str, *, sandbox_uid: str | None = None) -> None:
    """Retain the last completed lifecycle boundary, including if create/delete fails."""
    output_dir.mkdir(parents=True, exist_ok=True)
    progress: ShutdownProgress = {"phase": phase}
    if sandbox_uid is not None:
        progress["sandbox_uid"] = sandbox_uid
    (output_dir / PROGRESS_FILENAME).write_text(json.dumps(progress, indent=2) + "\n")


def _exact_lines(path: Path, marker: bytes) -> list[str]:
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return []
    return [
        line.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8", errors="replace")
        for line in contents.splitlines(keepends=True)
        if line.endswith(b"\n") and line.removesuffix(b"\n").removesuffix(b"\r") == marker
    ]


def _powerdown_lines(path: Path) -> list[str]:
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return []
    # Kernel printk adds uptime/log metadata; retain that exact line and match its final message.
    return [
        line.decode("utf-8", errors="replace").rstrip("\r\n")
        for line in contents.splitlines(keepends=True)
        if line.endswith(b"\n") and line.rstrip(b"\r\n").endswith(DOMAIN_POWERDOWN)
    ]


def _ordered_shutdown_proof(serial_files: list[Path]) -> bool:
    """Require all three markers in order in one raw serial stream."""
    for path in serial_files:
        try:
            lines = path.read_bytes().splitlines(keepends=True)
        except FileNotFoundError:
            continue
        positions: dict[bytes, list[int]] = {ACPI_REQUESTED: [], SHUTDOWN_COMPLETE: [], DOMAIN_POWERDOWN: []}
        for index, line in enumerate(lines):
            if not line.endswith(b"\n"):
                continue
            clean = line.rstrip(b"\r\n")
            if clean in (ACPI_REQUESTED, SHUTDOWN_COMPLETE):
                positions[clean].append(index)
            elif clean.endswith(DOMAIN_POWERDOWN):
                positions[DOMAIN_POWERDOWN].append(index)
        if any(
            acpi < complete < powerdown
            for acpi in positions[ACPI_REQUESTED]
            for complete in positions[SHUTDOWN_COMPLETE]
            for powerdown in positions[DOMAIN_POWERDOWN]
        ):
            return True
    return False


def _artifact_paths(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _cri_entry(line: bytes) -> dict[str, Any] | None:
    fields = line.rstrip(b"\r\n").split(b" ", 3)
    if len(fields) != 4 or fields[2] != b"F":
        return None
    try:
        entry = json.loads(fields[3])
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


def _domain_events(artifact_root: Path, vmi_uid: str) -> HostLifecycle:
    """Correlate exact retained KubeVirt log lines to the VMI UID from raw serial's path."""
    event_markers: tuple[tuple[DomainEventName, str, str], ...] = (
        ("grace_period_expired", "virt-handler", "Grace period expired, killing deleted VirtualMachineInstance"),
        ("virt_launcher_kill", "virt-launcher", "Signaled vmi kill"),
        (
            "domain_stopped_destroyed",
            "virt-launcher",
            'DomainLifecycle event Domain event="stopped" detail="destroyed"',
        ),
        ("domain_shutdown", "virt-launcher", 'DomainLifecycle event Domain event="shutdown"'),
        ("domain_shutoff_shutdown", "virt-handler", "Domain is in state Shutoff reason Shutdown"),
    )
    events: DomainEvents = {
        "grace_period_expired": [],
        "virt_launcher_kill": [],
        "domain_stopped_destroyed": [],
        "domain_shutdown": [],
        "domain_shutoff_shutdown": [],
    }
    correlated_entries: CorrelatedHostEntries = {"handlers": 0, "launchers": 0}
    log_sources: tuple[tuple[HostLogRoot, str], ...] = (("handlers", "virt-handler"), ("launchers", "virt-launcher"))
    for root_name, component in log_sources:
        for path in (artifact_root / root_name).rglob("*.log*"):
            try:
                lines = path.read_bytes().splitlines(keepends=True)
            except FileNotFoundError:
                continue
            for line in lines:
                entry = _cri_entry(line)
                if entry is None or entry.get("uid") != vmi_uid:
                    continue
                correlated_entries[root_name] += 1
                message = entry.get("msg")
                if not isinstance(message, str):
                    continue
                for name, expected_component, marker in event_markers:
                    if component == expected_component and marker in message:
                        events[name].append(line.decode("utf-8", errors="replace").rstrip("\r\n"))
    observation: HostLogObservation = (
        "correlated_host_logs_present"
        if all(correlated_entries.values())
        else "correlated_handler_or_launcher_logs_missing"
    )
    return {"observation": observation, "correlated_entries": correlated_entries, "events": events}


def record(artifact_root: Path, output_dir: Path, namespace: str) -> ShutdownDiagnosticReport:
    """Write classifications and exact correlated host/guest evidence paths and lines."""
    artifact_root.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / PROGRESS_FILENAME
    progress: ShutdownProgress = (
        json.loads(progress_path.read_text()) if progress_path.is_file() else {"phase": "not_started"}
    )
    serial_root = artifact_root / "serial"
    guests: list[GuestReport] = []
    for launcher in sorted(serial_root.glob(f"{namespace}_virt-launcher-*")):
        serial_files = sorted(launcher.rglob("*.log"))
        vmi_uids = sorted({path.parent.name for path in serial_files})
        vmi_uid = vmi_uids[0] if len(vmi_uids) == 1 else None
        guests.append(
            {
                "launcher": launcher.name,
                "vmi_uid": vmi_uid,
                "serial_artifacts": [path.relative_to(artifact_root).as_posix() for path in serial_files],
                "acpi_request_lines": [line for path in serial_files for line in _exact_lines(path, ACPI_REQUESTED)],
                "shutdown_complete_lines": [
                    line for path in serial_files for line in _exact_lines(path, SHUTDOWN_COMPLETE)
                ],
                "domain_powerdown_lines": [line for path in serial_files for line in _powerdown_lines(path)],
                "ordered_shutdown_proof": _ordered_shutdown_proof(serial_files),
                "host_lifecycle": _domain_events(artifact_root, vmi_uid) if vmi_uid is not None else None,
            }
        )

    classification: ShutdownClassification
    if progress["phase"] == "not_started":
        classification = "fixture_setup_failed_before_test"
    elif progress["phase"] == "started":
        guest_artifacts = any(
            _artifact_paths(artifact_root / name) for name in ("serial", "consoles", "launchers", "virtiofs")
        )
        classification = (
            "guest_launch_seen_before_ready"
            if guest_artifacts
            else "sandbox_creation_or_guest_launch_not_observed_before_ready"
        )
    elif progress["phase"] != "sandbox_deleted":
        classification = "sandbox_deletion_not_completed"
    elif len(guests) != 1:
        classification = "unexpected_guest_count"
    else:
        guest = guests[0]
        has_request = bool(guest["acpi_request_lines"])
        has_proof = bool(guest["shutdown_complete_lines"])
        has_powerdown = bool(guest["domain_powerdown_lines"])
        lifecycle = guest["host_lifecycle"]
        forced_stop = bool(
            lifecycle
            and (
                lifecycle["events"]["grace_period_expired"]
                or lifecycle["events"]["virt_launcher_kill"]
                or lifecycle["events"]["domain_stopped_destroyed"]
            )
        )
        if not has_request:
            classification = "guest_ready_acpi_receipt_missing"
        elif not has_proof:
            classification = "acpi_received_shutdown_proof_missing"
        elif not has_powerdown or not guest["ordered_shutdown_proof"]:
            classification = "shutdown_markers_missing_or_out_of_order"
        elif forced_stop:
            classification = "guest_powerdown_with_host_forced_stop"
        elif lifecycle is None or lifecycle["observation"] != "correlated_host_logs_present":
            classification = "guest_powerdown_host_lifecycle_logs_missing"
        elif not (lifecycle["events"]["domain_shutdown"] or lifecycle["events"]["domain_shutoff_shutdown"]):
            classification = "guest_powerdown_host_shutdown_event_missing"
        else:
            classification = "orderly_guest_powerdown"

    report: ShutdownDiagnosticReport = {
        "classification": classification,
        "test_identity": {
            key: os.environ[key]
            for key in ("TEST_TARGET", "TEST_WORKSPACE", "TEST_SHARD_INDEX", "TEST_RUN_NUMBER")
            if key in os.environ
        },
        "lifecycle": progress,
        "expected_guest_count": 1,
        "guest_count": len(guests),
        "guests": guests,
        "launcher_artifacts": _artifact_paths(artifact_root / "launchers"),
        "handler_artifacts": _artifact_paths(artifact_root / "handlers"),
        "virtiofs_artifacts": _artifact_paths(artifact_root / "virtiofs"),
        "host_qemu_artifacts": _artifact_paths(artifact_root / "host-qemu"),
    }
    (artifact_root / REPORT_FILENAME).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def require_orderly_shutdown(report: ShutdownDiagnosticReport) -> None:
    """Require Sandbox deletion, ordered guest evidence and a positive host shutdown event."""
    classification = report["classification"]
    if classification != "orderly_guest_powerdown":
        raise RuntimeError(
            f"VM shutdown diagnostic classified {classification}; "
            f"see vm-artifacts/{REPORT_FILENAME}, launchers/, handlers/, virtiofs/ and host-qemu/"
        )
