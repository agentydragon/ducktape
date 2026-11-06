"""Record lifecycle, guest shutdown and KubeVirt domain evidence for one VM."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ACPI_REQUESTED = b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED"
SHUTDOWN_COMPLETE = b"AGENTPLANE_GUEST_SHUTDOWN_COMPLETE"
DOMAIN_POWERDOWN = b"reboot: Power down"
PROGRESS_FILENAME = "vm-shutdown-diagnostic-progress.json"
REPORT_FILENAME = "shutdown-diagnostic.json"


def write_progress(output_dir: Path, phase: str, *, sandbox_uid: str | None = None) -> None:
    """Retain the last completed lifecycle boundary, including if create/delete fails."""
    output_dir.mkdir(parents=True, exist_ok=True)
    progress: dict[str, str] = {"phase": phase}
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


def _domain_events(artifact_root: Path, vmi_uid: str) -> dict[str, Any]:
    """Correlate exact retained KubeVirt log lines to the VMI UID from raw serial's path."""
    event_markers = {
        "grace_period_expired": ("virt-handler", "Grace period expired, killing deleted VirtualMachineInstance"),
        "virt_launcher_kill": ("virt-launcher", "Signaled vmi kill"),
        "domain_stopped_destroyed": (
            "virt-launcher",
            'DomainLifecycle event Domain event="stopped" detail="destroyed"',
        ),
        "domain_shutdown": ("virt-launcher", 'DomainLifecycle event Domain event="shutdown"'),
        "domain_shutoff_shutdown": ("virt-handler", "Domain is in state Shutoff reason Shutdown"),
    }
    events = {name: [] for name in event_markers}
    correlated_entries = {"handlers": 0, "launchers": 0}
    for root_name, component in (("handlers", "virt-handler"), ("launchers", "virt-launcher")):
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
                for name, (expected_component, marker) in event_markers.items():
                    if component == expected_component and marker in message:
                        events[name].append(line.decode("utf-8", errors="replace").rstrip("\r\n"))
    observation = (
        "correlated_host_logs_present"
        if all(correlated_entries.values())
        else "correlated_handler_or_launcher_logs_missing"
    )
    return {"observation": observation, "correlated_entries": correlated_entries, "events": events}


def record(artifact_root: Path, output_dir: Path, namespace: str) -> dict[str, Any]:
    """Write classifications and exact correlated host/guest evidence paths and lines."""
    artifact_root.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / PROGRESS_FILENAME
    progress = json.loads(progress_path.read_text()) if progress_path.is_file() else {"phase": "not_started"}
    serial_root = artifact_root / "serial"
    guests = []
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

    report = {
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


def require_orderly_shutdown(report: dict[str, Any]) -> None:
    """Require Sandbox deletion, ordered guest evidence and a positive host shutdown event."""
    classification = report["classification"]
    if classification != "orderly_guest_powerdown":
        raise RuntimeError(
            f"VM shutdown diagnostic classified {classification}; "
            f"see vm-artifacts/{REPORT_FILENAME}, launchers/, handlers/, virtiofs/ and host-qemu/"
        )
