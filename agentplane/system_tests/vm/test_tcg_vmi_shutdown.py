"""Manual KubeVirt/TCG shutdown diagnostic without the Agentplane service stack."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import App, Chart, Testing as Cdk8sTesting
from python.runfiles import Runfiles

from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.vm import boot_rcu, guest_logs, host_qemu, infrastructure, shutdown_diagnostic
from cluster.cdk8s.agentplane import launcher_relay
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

_NAMESPACE_PREFIX = "vm-tcg-shutdown-"
_OWNER_LABEL = "agentplane.io/diagnostic"
_OWNER_NAMESPACE_LABEL = "agentplane.io/diagnostic-namespace"
_VMI_NAME = "ci-guest"
_STATE_PVC_NAME = "ci-guest-state"
_POLICY_SUFFIX = "-virtiofs-shutdown"
_BOOT_EVIDENCE = "AGENTPLANE_BOOT_EVIDENCE_END status=complete"
_VIRSH_URI = "qemu+unix:///session?socket=/var/run/libvirt/virtqemud-sock"
_BOOT_WAIT_SECONDS = 240
_SHUTDOWN_WAIT_SECONDS = 180
_SKIP_VIRTIOFS_POLICY_ENV = "AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY"
_CYCLES_ENV = "AGENTPLANE_TCG_VMI_CYCLES"
_DEBUG_KERNEL_ARGS_ENV = "AGENTPLANE_TCG_VMI_DEBUG_KERNEL_ARGS"
_DEBUG_KERNEL_ARGS = "systemd.log_level=debug systemd.log_target=console loglevel=8"
_BOOT_RCU_ENV = "AGENTPLANE_TCG_VMI_BOOT_RCU_OBSERVER"
_SYNTHETIC_SLICE_ENV = "AGENTPLANE_TCG_VMI_SYNTHETIC_SLICE_PROBE"
_SYNTHETIC_SLICE_SUCCESS = "AGENTPLANE_SYNTHETIC_SLICE_SUCCESS"
_SYNTHETIC_SLICE_WAIT_SECONDS = 90
_CGROUP_PILOT_ENV = "AGENTPLANE_TCG_VMI_CGROUP_PILOT"
_CGROUP_PILOT_WAIT_SECONDS = 120
_CGROUP_PILOT_STEPS = ("pair1_on", "pair1_off", "pair2_on", "pair2_off")
_CGROUP_STRESS_ENV = "AGENTPLANE_TCG_VMI_CGROUP_STRESS"
_CGROUP_STRESS_PAIRS = 5
_CGROUP_STRESS_STEPS = tuple(f"pair{pair}_{direction}" for pair in range(1, 6) for direction in ("on", "off"))
_DELEGATED_CGROUP_STRESS_ENV = "AGENTPLANE_TCG_VMI_DELEGATED_CGROUP_STRESS"
_DELEGATED_CGROUP_STRESS_PAIRS = 20
_DELEGATED_CGROUP_STRESS_STEPS = tuple(
    f"pair{pair}_{direction}" for pair in range(1, _DELEGATED_CGROUP_STRESS_PAIRS + 1) for direction in ("on", "off")
)
_DELEGATED_CGROUP_STRESS_WAIT_SECONDS = 150


def _cycle_count() -> int:
    value = os.environ.get(_CYCLES_ENV, "1")
    try:
        count = int(value)
    except ValueError as error:
        raise ValueError(f"{_CYCLES_ENV} must be an integer from 1 to 3, got {value!r}") from error
    if count not in range(1, 4):
        raise ValueError(f"{_CYCLES_ENV} must be an integer from 1 to 3, got {value!r}")
    return count


def _debug_kernel_args() -> bool:
    match os.environ.get(_DEBUG_KERNEL_ARGS_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_DEBUG_KERNEL_ARGS_ENV} must be '0' or '1', got {value!r}")


def _boot_rcu_observer() -> bool:
    match os.environ.get(_BOOT_RCU_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_BOOT_RCU_ENV} must be '0' or '1', got {value!r}")


def _synthetic_slice_probe() -> bool:
    match os.environ.get(_SYNTHETIC_SLICE_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_SYNTHETIC_SLICE_ENV} must be '0' or '1', got {value!r}")


def _cgroup_pilot() -> bool:
    match os.environ.get(_CGROUP_PILOT_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_CGROUP_PILOT_ENV} must be '0' or '1', got {value!r}")


def _cgroup_stress() -> bool:
    match os.environ.get(_CGROUP_STRESS_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_CGROUP_STRESS_ENV} must be '0' or '1', got {value!r}")


def _delegated_cgroup_stress() -> bool:
    match os.environ.get(_DELEGATED_CGROUP_STRESS_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_DELEGATED_CGROUP_STRESS_ENV} must be '0' or '1', got {value!r}")


def _skip_virtiofs_shutdown_policy() -> bool:
    """Skip only the namespace-scoped virtiofs native-sidecar rule when explicitly requested."""
    match os.environ.get(_SKIP_VIRTIOFS_POLICY_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_SKIP_VIRTIOFS_POLICY_ENV} must be '0' or '1', got {value!r}")


def _layout_paths(runfiles: Runfiles, images: dict[str, str]) -> dict[str, Path]:
    root = runfiles.root()
    return {reference: root / path for reference, path in images.items()}


def _create(k3s: baseline.K3s, resource: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(k3s.kubectl("create", "-f", "-", "-o", "json", input_text=json.dumps(resource), stdout_only=True)),
    )


def _owner_value(namespace: str) -> str:
    return namespace.removeprefix(_NAMESPACE_PREFIX)


def _namespace(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": {_OWNER_LABEL: _owner_value(name)}},
    }


def _placeholder_config_map(namespace: str, name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": {_OWNER_LABEL: _owner_value(namespace)}},
        "data": {"placeholder.txt": "unused by this shutdown-only diagnostic\n"},
    }


def _state_claim(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _STATE_PVC_NAME,
            "namespace": namespace,
            "labels": {_OWNER_LABEL: _owner_value(namespace)},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "local-path",
            "resources": {"requests": {"storage": "1Gi"}},
            "volumeMode": "Filesystem",
        },
    }


def _virtiofs_shutdown_policy(namespace: str) -> dict[str, Any]:
    """Install only the real launcher's generated virtiofs native-sidecar mutation, scoped to this test."""
    chart = Chart(App(), "standalone-vmi-tcg")
    runner_pod = {
        "spec": {
            "initContainers": [
                {"name": launcher_relay.sidecar_config.CONTAINER_NAME, "securityContext": {}, "volumeMounts": []}
            ],
            "volumes": [
                {
                    "name": launcher_relay.sandbox_pod.EGRESS_TOKEN_VOLUME_NAME,
                    "projected": {"sources": [{"serviceAccountToken": {"audience": "unused", "path": "token"}}]},
                },
                {
                    "name": launcher_relay.sandbox_pod.SIDECAR_SETTINGS_VOLUME_NAME,
                    "configMap": {"name": "unused-by-virtiofs-only-policy"},
                },
            ],
        }
    }
    launcher_relay.LauncherRelayPolicy(
        chart,
        "launcher-relay",
        namespace=namespace,
        runner_pod=runner_pod,
        settings_config_map="unused-by-virtiofs-only-policy",
    )
    policy = cast(dict[str, Any], Cdk8sTesting.synth(chart)[0])
    policy["metadata"]["name"] = namespace + _POLICY_SUFFIX
    policy["metadata"].setdefault("labels", {}).update(
        {_OWNER_LABEL: _owner_value(namespace), _OWNER_NAMESPACE_LABEL: namespace}
    )
    rule = next(rule for rule in policy["spec"]["rules"] if rule["name"] == "keep-virtiofs-through-guest-shutdown")
    # The direct VMI has no VirtualMachine/Sandbox chain. Keep the production rule's exact
    # VMI lookup and mutation while excluding relay authentication/injection and unrelated rules.
    rule["context"] = [entry for entry in rule["context"] if entry["name"] in {"vmiOwner", "vmi"}]
    return {**policy, "spec": {**policy["spec"], "rules": [rule]}}


def _create_cluster_policy(k3s: baseline.K3s, namespace: str) -> dict[str, Any]:
    resource = _virtiofs_shutdown_policy(namespace)
    try:
        return _create(k3s, resource)
    except Exception:
        # Recover only this random, namespace-scoped policy if the API committed it before
        # kubectl lost its response.
        current = k3s.kubectl(
            "get",
            "clusterpolicy",
            resource["metadata"]["name"],
            "-o",
            "json",
            "--ignore-not-found=true",
            check=False,
            stdout_only=True,
        )
        if not current:
            raise
        policy = json.loads(current)
        metadata = policy["metadata"]
        if (
            metadata.get("labels", {}).get(_OWNER_LABEL) != _owner_value(namespace)
            or metadata.get("labels", {}).get(_OWNER_NAMESPACE_LABEL) != namespace
        ):
            raise RuntimeError(f"refusing create recovery for unowned ClusterPolicy {metadata.get('name')}")
        return cast(dict[str, Any], policy)


def _disk(name: str, serial: str) -> dict[str, Any]:
    return {"name": name, "serial": serial, "disk": {"bus": "virtio"}}


def _vmi(
    namespace: str,
    images: dict[str, str],
    *,
    debug_kernel_args: bool = False,
    boot_rcu_observe: bool = False,
    synthetic_slice_probe: bool = False,
    cgroup_pilot: bool = False,
    cgroup_stress: bool = False,
    delegated_cgroup_stress: bool = False,
) -> dict[str, Any]:
    boot_image = next(reference for reference in images if reference.endswith("agentplane-vm-guest-boot:unset"))
    root_image = next(reference for reference in images if reference.endswith("agentplane-vm-guest-root:unset"))
    runner_image = next(reference for reference in images if reference.endswith("agentplane-runner-disk:unset"))
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachineInstance",
        "metadata": {
            "name": _VMI_NAME,
            "namespace": namespace,
            "labels": {_OWNER_LABEL: _owner_value(namespace), "kubevirt.io/domain": _VMI_NAME},
        },
        "spec": {
            "terminationGracePeriodSeconds": 30,
            "domain": {
                "cpu": {"cores": 2},
                "resources": {"requests": {"memory": "1536Mi"}},
                "firmware": {
                    "kernelBoot": {
                        "kernelArgs": (
                            "console=ttyS0 net.ifnames=0 agentplane.boot-evidence=1 "
                            "systemd.mask=agentplane-environment-runtime.service"
                            + (f" {_DEBUG_KERNEL_ARGS}" if debug_kernel_args else "")
                            + (" agentplane.boot-rcu-observe=1" if boot_rcu_observe else "")
                            + (" agentplane.synthetic-slice-probe=1" if synthetic_slice_probe else "")
                            + (" agentplane.cgroup-pilot=1" if cgroup_pilot else "")
                            + (" agentplane.cgroup-stress=1" if cgroup_stress else "")
                            + (" agentplane.delegated-cgroup-stress=1" if delegated_cgroup_stress else "")
                        ),
                        "container": {
                            "image": boot_image,
                            "kernelPath": "/boot/vmlinuz",
                            "initrdPath": "/boot/initramfs.cpio",
                            "imagePullPolicy": "IfNotPresent",
                        },
                    }
                },
                "devices": {
                    "autoattachGraphicsDevice": False,
                    "disks": [
                        _disk("rootdisk", "guest-root"),
                        _disk("runner", "agentplane-runner"),
                        _disk("workspace", "workspace"),
                        _disk("config", "agentplane-config"),
                        _disk("launch", "agentplane-launch"),
                        _disk("trust", "agentplane-trust"),
                    ],
                    "filesystems": [{"name": "state", "virtiofs": {}}],
                    "interfaces": [{"name": "default", "masquerade": {}}],
                },
            },
            "networks": [{"name": "default", "pod": {}}],
            "volumes": [
                {"name": "rootdisk", "containerDisk": {"image": root_image, "imagePullPolicy": "IfNotPresent"}},
                {"name": "runner", "containerDisk": {"image": runner_image, "imagePullPolicy": "IfNotPresent"}},
                {"name": "workspace", "emptyDisk": {"capacity": "1Gi"}},
                {"name": "config", "configMap": {"name": "ci-guest-config"}},
                {"name": "launch", "configMap": {"name": "ci-guest-launch"}},
                {"name": "trust", "configMap": {"name": "ci-guest-trust"}},
                {"name": "state", "persistentVolumeClaim": {"claimName": _STATE_PVC_NAME}},
            ],
        },
    }


def _wait_for_pvc(k3s: baseline.K3s, namespace: str) -> dict[str, Any]:
    k3s.kubectl(
        "wait", "--for=jsonpath={.status.phase}=Bound", f"pvc/{_STATE_PVC_NAME}", "-n", namespace, "--timeout=120s"
    )
    return cast(dict[str, Any], json.loads(k3s.kubectl("get", "pvc", _STATE_PVC_NAME, "-n", namespace, "-o", "json")))


def _launcher(k3s: baseline.K3s, namespace: str) -> dict[str, Any]:
    pods = json.loads(
        k3s.kubectl(
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"kubevirt.io=virt-launcher,kubevirt.io/domain={_VMI_NAME}",
            "-o",
            "json",
        )
    )["items"]
    if len(pods) != 1:
        raise RuntimeError(f"expected one launcher pod for the diagnostic VMI, found {len(pods)}")
    return cast(dict[str, Any], pods[0])


def _launcher_lifecycle_sample(k3s: baseline.K3s, namespace: str, pod_name: str, expected_uid: str) -> dict[str, Any]:
    raw = k3s.kubectl(
        "get", "pod", pod_name, "-n", namespace, "--ignore-not-found=true", "-o", "json", timeout=10, stdout_only=True
    )
    if not raw:
        return {"pod_absent": True}
    pod = json.loads(raw)
    metadata = pod.get("metadata", {})
    actual_uid = metadata.get("uid")
    if actual_uid != expected_uid:
        raise RuntimeError(f"launcher Pod UID changed during lifecycle capture: {actual_uid!r} != {expected_uid!r}")
    status = pod.get("status", {})
    container_statuses = {
        field: [
            {
                "name": item.get("name"),
                "ready": item.get("ready"),
                "restartCount": item.get("restartCount"),
                "state": item.get("state", {}),
                "lastState": item.get("lastState", {}),
            }
            for item in status.get(field, [])
        ]
        for field in ("initContainerStatuses", "containerStatuses")
    }
    return {
        "pod_absent": False,
        "pod_uid": actual_uid,
        "deletion_timestamp": metadata.get("deletionTimestamp"),
        "phase": status.get("phase"),
        **container_statuses,
    }


def _start_launcher_lifecycle_capture(
    k3s: baseline.K3s, namespace: str, pod_name: str, pod_uid: str, destination: Path
) -> tuple[threading.Event, threading.Thread]:
    """Poll only this launcher Pod's lifecycle fields while ordinary VMI deletion waits."""
    stop = threading.Event()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("")

    def write_record(record: dict[str, Any]) -> None:
        record["observed_at_utc"] = datetime.now(UTC).isoformat()
        with destination.open("a") as output:
            output.write(json.dumps(record, sort_keys=True) + "\n")

    def collect() -> None:
        deadline = time.monotonic() + _SHUTDOWN_WAIT_SECONDS + 30
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                sample = _launcher_lifecycle_sample(k3s, namespace, pod_name, pod_uid)
                write_record(sample)
                if sample["pod_absent"]:
                    return
            except Exception as error:
                write_record({"capture_error": repr(error)})
                return
            stop.wait(2)

    thread = threading.Thread(target=collect, name="vm-launcher-lifecycle-capture", daemon=True)
    thread.start()
    return stop, thread


def _stop_launcher_lifecycle_capture(stop: threading.Event, thread: threading.Thread) -> None:
    stop.set()
    thread.join(timeout=12)
    if thread.is_alive():
        raise TimeoutError("launcher lifecycle observer did not stop within its bounded kubectl timeout")


def _count_guest_payload(consoles: Path, payload: bytes) -> int:
    return sum(
        1
        for path in consoles.glob("*/*.cri.log")
        for line in path.read_bytes().splitlines(keepends=True)
        if line.endswith(b"\n") and line.rstrip(b"\r\n").split(b" ", 3)[1:] == [b"stdout", b"F", payload]
    )


def _write_shutdown_request_count(output: Path) -> int:
    consoles = output / "vm-artifacts" / "consoles"
    try:
        count = _count_guest_payload(consoles, host_qemu.SHUTDOWN_REQUESTED)
        result: dict[str, Any] = {
            "marker": host_qemu.SHUTDOWN_REQUESTED.decode(),
            "observed_guest_acpi_request_markers": count,
            "meaning": "count of exact guest-console CRI payloads emitted by the ACPI poweroff handler",
            "qmp_system_powerdown_commands_observed": False,
        }
    except Exception as error:
        count = 0
        result = {"capture_error": repr(error), "qmp_system_powerdown_commands_observed": False}
    (output / "guest-shutdown-request-count.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return count


def _require_shutdown_proof_progress(consoles: Path, output: Path) -> None:
    """Require direct-TTY progress markers from a complete healthy guest shutdown."""
    records: list[tuple[str, bytes]] = []
    for path in sorted(consoles.glob("*/*.cri.log")):
        for line in path.read_bytes().splitlines(keepends=True):
            if not line.endswith(b"\n"):
                continue
            fields = line.rstrip(b"\r\n").split(b" ", 3)
            if len(fields) == 4 and fields[1:3] == [b"stdout", b"F"]:
                records.append((fields[0].decode("ascii", errors="replace"), fields[3]))
    records.sort(key=lambda record: record[0])
    payloads = [payload for _, payload in records]
    expected_markers = [
        (b"AGENTPLANE_SHUTDOWN_PROOF_ENTER", True),
        (b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_BEGIN mountpoint=/state/workspaces", True),
        (b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_RESULT mountpoint=/state/workspaces findmnt_status=", False),
        (b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_BEGIN mountpoint=/state", True),
        (b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_RESULT mountpoint=/state findmnt_status=", False),
        (b"AGENTPLANE_GUEST_SHUTDOWN_COMPLETE", True),
        (b"AGENTPLANE_SHUTDOWN_PROOF_EXIT status=0", True),
    ]
    marker_positions: list[int] = []
    errors: list[str] = []
    for marker, exact_match in expected_markers:
        matches = [
            index
            for index, payload in enumerate(payloads)
            if (payload == marker if exact_match else payload.startswith(marker))
        ]
        if len(matches) != 1:
            errors.append(f"{marker.decode()}: expected one record, found {len(matches)}")
            continue
        marker_positions.append(matches[0])

    results: dict[str, list[bytes]] = {}
    for mountpoint_bytes in (b"/state/workspaces", b"/state"):
        prefix = b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_RESULT mountpoint=" + mountpoint_bytes + b" findmnt_status="
        results[mountpoint_bytes.decode()] = [payload for payload in payloads if payload.startswith(prefix)]
    for mountpoint_name, markers in results.items():
        if len(markers) == 1:
            status = markers[0].rsplit(b"=", 1)[-1]
            if not status.isdigit() or int(status) == 0:
                errors.append(f"findmnt for {mountpoint_name} did not report an unmounted path: {markers[0]!r}")
    if len(marker_positions) == len(expected_markers) and marker_positions != sorted(marker_positions):
        errors.append(f"proof markers are out of order: {marker_positions!r}")

    report = {
        "records": [
            payload.decode("utf-8", errors="replace")
            for payload in payloads
            if payload.startswith((b"AGENTPLANE_SHUTDOWN_PROOF_", b"AGENTPLANE_GUEST_SHUTDOWN_COMPLETE"))
        ],
        "expected_marker_order": [marker.decode() for marker, _ in expected_markers],
        "marker_positions": marker_positions,
        "errors": errors,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "shutdown-proof-progress.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if errors:
        raise AssertionError("guest shutdown proof progress was incomplete or invalid: " + "; ".join(errors))


def _write_proof_console(consoles: Path, payloads: list[bytes]) -> None:
    console = consoles / "launcher" / "console.cri.log"
    console.parent.mkdir(parents=True, exist_ok=True)
    console.write_bytes(
        b"".join(
            f"2026-11-06T12:00:00.{index:09d}Z stdout F ".encode() + payload + b"\n"
            for index, payload in enumerate(payloads)
        )
    )


def _healthy_proof_payloads() -> list[bytes]:
    return [
        b"AGENTPLANE_SHUTDOWN_PROOF_ENTER",
        b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_BEGIN mountpoint=/state/workspaces",
        b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_RESULT mountpoint=/state/workspaces findmnt_status=1",
        b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_BEGIN mountpoint=/state",
        b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_RESULT mountpoint=/state findmnt_status=1",
        b"AGENTPLANE_GUEST_SHUTDOWN_COMPLETE",
        b"AGENTPLANE_SHUTDOWN_PROOF_EXIT status=0",
    ]


def test_shutdown_proof_progress_matches_both_mountpoints_exactly(tmp_path: Path) -> None:
    consoles = tmp_path / "consoles"
    _write_proof_console(consoles, _healthy_proof_payloads())

    _require_shutdown_proof_progress(consoles, tmp_path)

    report = json.loads((tmp_path / "shutdown-proof-progress.json").read_text())
    assert report["errors"] == []
    assert report["marker_positions"] == list(range(7))


def test_shutdown_proof_progress_rejects_duplicate_or_missing_mount_markers(tmp_path: Path) -> None:
    consoles = tmp_path / "consoles"
    payloads = _healthy_proof_payloads()
    payloads.insert(4, b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_BEGIN mountpoint=/state")
    payloads.remove(b"AGENTPLANE_SHUTDOWN_PROOF_CHECK_RESULT mountpoint=/state findmnt_status=1")
    _write_proof_console(consoles, payloads)

    with pytest.raises(AssertionError, match="expected one record, found 2"):
        _require_shutdown_proof_progress(consoles, tmp_path)

    report = json.loads((tmp_path / "shutdown-proof-progress.json").read_text())
    assert any("expected one record, found 0" in error for error in report["errors"])


def test_shutdown_proof_progress_rejects_out_of_order_markers(tmp_path: Path) -> None:
    consoles = tmp_path / "consoles"
    payloads = _healthy_proof_payloads()
    payloads[1], payloads[2] = payloads[2], payloads[1]
    _write_proof_console(consoles, payloads)

    with pytest.raises(AssertionError, match="proof markers are out of order"):
        _require_shutdown_proof_progress(consoles, tmp_path)


def _wait_for_guest_boot(k3s: baseline.K3s, namespace: str, pod: str) -> str:
    deadline = time.monotonic() + _BOOT_WAIT_SECONDS
    output = ""
    while time.monotonic() < deadline:
        output = k3s.kubectl("logs", pod, "-n", namespace, "-c", "guest-console-log", check=False)
        if _BOOT_EVIDENCE in output:
            return output
        time.sleep(1)
    raise TimeoutError(
        f"guest did not reach completed boot evidence within {_BOOT_WAIT_SECONDS}s; "
        f"last console output:\n{output[-8000:]}"
    )


def _synthetic_slice_complete(console: str) -> bool:
    phases = ("baseline", "active", "after")
    next_phase = 0
    for line in console.splitlines():
        record = line.rstrip("\r")
        if next_phase < len(phases) and record.startswith(f"AGENTPLANE_SYNTHETIC_SLICE_PHASE={phases[next_phase]} "):
            next_phase += 1
        elif next_phase == len(phases) and record == _SYNTHETIC_SLICE_SUCCESS:
            return True
    return False


def _wait_for_synthetic_slice(k3s: baseline.K3s, namespace: str, pod: str, output: Path) -> None:
    deadline = time.monotonic() + _SYNTHETIC_SLICE_WAIT_SECONDS
    while time.monotonic() < deadline:
        console = k3s.kubectl(
            "logs", pod, "-n", namespace, "-c", "guest-console-log", "--limit-bytes=262144", check=False
        )
        output.write_text(console)
        if _synthetic_slice_complete(console):
            return
        time.sleep(1)
    raise TimeoutError(
        f"synthetic slice did not complete baseline/active/after/success milestones "
        f"within {_SYNTHETIC_SLICE_WAIT_SECONDS}s; see {output}"
    )


def _cgroup_pilot_complete(console: str) -> bool:
    records = [line.rstrip("\r") for line in console.splitlines()]
    if any(record.startswith("AGENTPLANE_CGROUP_PILOT_UNEXERCISED ") for record in records):
        return False
    next_step = 0
    for record in records:
        if next_step < len(_CGROUP_PILOT_STEPS) and record.startswith(
            f"AGENTPLANE_CGROUP_PILOT_RESULT step={_CGROUP_PILOT_STEPS[next_step]} "
        ):
            next_step += 1
        elif next_step == len(_CGROUP_PILOT_STEPS) and record.startswith(
            "AGENTPLANE_CGROUP_PILOT_SUCCESS transitions=4 "
        ):
            return True
    return False


def _wait_for_cgroup_pilot(k3s: baseline.K3s, namespace: str, pod: str, output: Path) -> None:
    deadline = time.monotonic() + _CGROUP_PILOT_WAIT_SECONDS
    while time.monotonic() < deadline:
        console = k3s.kubectl(
            "logs", pod, "-n", namespace, "-c", "guest-console-log", "--limit-bytes=262144", check=False
        )
        output.write_text(console)
        if any(line.rstrip("\r").startswith("AGENTPLANE_CGROUP_PILOT_UNEXERCISED ") for line in console.splitlines()):
            raise RuntimeError(f"cgroup pilot did not exercise the requested transition; see {output}")
        if _cgroup_pilot_complete(console):
            return
        time.sleep(1)
    raise TimeoutError(
        f"cgroup pilot did not complete four ordered property changes within {_CGROUP_PILOT_WAIT_SECONDS}s; see {output}"
    )


def _cgroup_stress_complete(console: str) -> bool:
    prefix = "AGENTPLANE_CGROUP_STRESS"
    records = [line.rstrip("\r") for line in console.splitlines()]
    if any(record.startswith(f"{prefix}_UNEXERCISED ") for record in records):
        return False
    if any(
        record.startswith((f"{prefix}_TRACE_CPU_STATS ", f"{prefix}_TRACE_INTEGRITY ", f"{prefix}_TRACE_CHAIN "))
        and " step=pair1_on " not in record
        for record in records
    ):
        return False

    cursor = -1

    def take(start: str) -> str | None:
        nonlocal cursor
        for index in range(cursor + 1, len(records)):
            if records[index].startswith(start):
                cursor = index
                return records[index]
        return None

    def matching_mask(record: str | None, expected_io: bool) -> bool:
        if record is None:
            return False
        fields = dict(field.split("=", 1) for field in record.split()[1:] if "=" in field)
        return fields.get("resident") == "yes" and all(
            ("io" in fields.get(mask, "").split(",")) is expected_io for mask in ("root", "slice")
        )

    if take(f"{prefix}_BEGIN pairs={_CGROUP_STRESS_PAIRS} direct_root_writes=none active_budget_seconds=75") is None:
        return False
    for step in _CGROUP_STRESS_STEPS:
        value = "yes" if step.endswith("_on") else "no"
        expected_io = value == "yes"
        if not matching_mask(take(f"{prefix}_SNAPSHOT step={step}_before "), not expected_io):
            return False
        if take(f"{prefix}_MASK_WAIT step={step} ") is None:
            return False
        if not matching_mask(take(f"{prefix}_MASK_CONVERGED step={step} "), expected_io):
            return False
        if step == "pair1_on":
            if take(f"{prefix}_TRACE_INTEGRITY step=pair1_on ") is None:
                return False
            if "counters=clean" not in records[cursor]:
                return False
            if take(f"{prefix}_TRACE_CHAIN step=pair1_on status=proven ") is None:
                return False
        if not matching_mask(take(f"{prefix}_SNAPSHOT step={step}_after "), expected_io):
            return False
        if take(f"{prefix}_RESULT step={step} value={value} property_status=0 observed_property={value} ") is None:
            return False
        if step == "pair1_on" and take(f"{prefix}_TRACE_PHASE after=pair1_on tracing_on=0 current_tracer=nop") is None:
            return False
    return take(f"{prefix}_SUCCESS transitions=10 ") is not None


def _wait_for_cgroup_stress(k3s: baseline.K3s, namespace: str, pod: str, output: Path) -> None:
    prefix = "AGENTPLANE_CGROUP_STRESS"
    deadline = time.monotonic() + _CGROUP_PILOT_WAIT_SECONDS
    while time.monotonic() < deadline:
        console = k3s.kubectl(
            "logs", pod, "-n", namespace, "-c", "guest-console-log", "--limit-bytes=262144", check=False
        )
        output.write_text(console)
        if any(line.rstrip("\r").startswith(f"{prefix}_UNEXERCISED ") for line in console.splitlines()):
            raise RuntimeError(f"cgroup stress did not exercise ten verified controller changes; see {output}")
        if _cgroup_stress_complete(console):
            return
        time.sleep(1)
    raise TimeoutError(
        f"cgroup stress did not complete ten verified controller changes within {_CGROUP_PILOT_WAIT_SECONDS}s; see {output}"
    )


def _delegated_cgroup_stress_complete(console: str) -> bool:
    prefix = "AGENTPLANE_DELEGATED_CGROUP_STRESS"
    records = [line.rstrip("\r") for line in console.splitlines()]
    if any(record.startswith(f"{prefix}_UNEXERCISED ") for record in records):
        return False

    def unique(start: str) -> tuple[int, str] | None:
        matches = [(index, record) for index, record in enumerate(records) if record.startswith(start)]
        return matches[0] if len(matches) == 1 else None

    def fields(record: str) -> dict[str, str]:
        return dict(field.split("=", 1) for field in record.split()[1:] if "=" in field)

    begin = unique(
        f"{prefix}_BEGIN pairs={_DELEGATED_CGROUP_STRESS_PAIRS} direct_root_writes=none "
        "delegated_parent=systemd_service_child max_active_seconds=75"
    )
    ready = unique(f"{prefix}_READY ")
    proof = unique(f"{prefix}_TRACE_PROOF ")
    trace_disabled = unique(f"{prefix}_TRACE_DISABLED tracing_on=0 current_tracer=nop before_stress=yes")
    cleanup = unique(f"{prefix}_CLEANUP owned_children_removed=yes resident_alive=no trace_restored=yes")
    success = unique(
        f"{prefix}_SUCCESS transitions={_DELEGATED_CGROUP_STRESS_PAIRS * 2} "
        "delegated_subtree_only=yes tracing_during_pairs=no"
    )
    if None in (begin, ready, proof, trace_disabled, cleanup, success):
        return False
    assert begin is not None
    assert ready is not None
    assert proof is not None
    assert trace_disabled is not None
    assert cleanup is not None
    assert success is not None

    ready_fields = fields(ready[1])
    if any(
        ready_fields.get(name) != "yes"
        for name in ("service_parent_empty", "manager_empty", "io_available", "writer_in_driver", "resident_in_payload")
    ):
        return False
    proof_fields = fields(proof[1])
    writer_pid = proof_fields.get("writer_pid", "")
    if not writer_pid.isdecimal() or proof_fields.get("pid_match") != "yes" or proof_fields.get("status") != "proven":
        return False
    path = proof_fields.get("path", "").split(">")
    required_path = ("cgroup_subtree_control_write", "cgroup_update_dfl_csses", "percpu_down_write", "rcu_sync_enter")
    cursor = -1
    for name in required_path:
        position = next(
            (
                index
                for index in range(cursor + 1, len(path))
                if path[index] == name or path[index].startswith(f"{name}.")
            ),
            -1,
        )
        if position < 0:
            return False
        cursor = position
    if (
        proof_fields.get("step") != "proof_on"
        or proof_fields.get("io") != "yes"
        or proof_fields.get("verified") != "yes"
    ):
        return False
    if proof_fields.get("trace_integrity") != "clean" or proof_fields.get("tree") != "1":
        return False
    resident_pid = ready_fields.get("resident_pid", "")
    if not resident_pid.isdecimal():
        return False

    expected_steps = ("proof_reset_off", *_DELEGATED_CGROUP_STRESS_STEPS)
    results = [(index, record) for index, record in enumerate(records) if record.startswith(f"{prefix}_RESULT ")]
    if len(results) != len(expected_steps):
        return False
    for (index, record), step in zip(results, expected_steps, strict=True):
        expected_io = "yes" if step.endswith("_on") else "no"
        if step == "proof_reset_off":
            expected_io = "no"
        result_fields = fields(record)
        if (
            result_fields.get("step") != step
            or result_fields.get("io") != expected_io
            or result_fields.get("verified") != "yes"
            or result_fields.get("writer_pid") != writer_pid
            or result_fields.get("resident_pid") != resident_pid
        ):
            return False
        if index <= trace_disabled[0]:
            return False

    return (
        begin[0] < ready[0] < proof[0] < trace_disabled[0] < results[0][0] and results[-1][0] < cleanup[0] < success[0]
    )


def _wait_for_delegated_cgroup_stress(k3s: baseline.K3s, namespace: str, pod: str, output: Path) -> None:
    prefix = "AGENTPLANE_DELEGATED_CGROUP_STRESS"
    deadline = time.monotonic() + _DELEGATED_CGROUP_STRESS_WAIT_SECONDS
    while time.monotonic() < deadline:
        console = k3s.kubectl(
            "logs", pod, "-n", namespace, "-c", "guest-console-log", "--limit-bytes=262144", check=False
        )
        output.write_text(console)
        if any(line.rstrip("\r").startswith(f"{prefix}_UNEXERCISED ") for line in console.splitlines()):
            raise RuntimeError(f"delegated cgroup stress did not prove the requested path; see {output}")
        if _delegated_cgroup_stress_complete(console):
            return
        time.sleep(1)
    raise TimeoutError(
        f"delegated cgroup stress did not complete {_DELEGATED_CGROUP_STRESS_PAIRS} verified pairs "
        f"within {_DELEGATED_CGROUP_STRESS_WAIT_SECONDS}s; see {output}"
    )


def _qemu_evidence(k3s: baseline.K3s, namespace: str, pod: str, output: Path) -> tuple[str, str]:
    compute = ["-n", namespace, "exec", pod, "-c", "compute", "--"]
    domain_list = k3s.kubectl(*compute, "virsh", "-c", _VIRSH_URI, "list", "--name").split()
    if len(domain_list) != 1:
        raise RuntimeError(f"expected exactly one libvirt domain in launcher, found {domain_list!r}")
    domain_xml = k3s.kubectl(*compute, "virsh", "-c", _VIRSH_URI, "dumpxml", domain_list[0])
    (output / "domain.xml").write_text(domain_xml)
    domain_type = ET.fromstring(domain_xml).attrib.get("type")
    if domain_type != "qemu":
        raise AssertionError(f"expected a software-emulated libvirt domain type=qemu, found {domain_type!r}")

    qmp_kvm = k3s.kubectl(
        *compute, "virsh", "-c", _VIRSH_URI, "qemu-monitor-command", domain_list[0], '{"execute":"query-kvm"}'
    )
    (output / "qmp-query-kvm.json").write_text(qmp_kvm + "\n")
    kvm_state = json.loads(qmp_kvm).get("return", {})
    if kvm_state.get("enabled") is not False:
        raise AssertionError(f"expected QEMU KVM acceleration to be disabled, query-kvm returned {qmp_kvm!r}")

    script = r"""for path in /proc/[0-9]*/cmdline; do
  comm_path=${path%cmdline}comm
  [ -r "$path" ] || continue
  IFS= read -r comm < "$comm_path" || continue
  case "$comm" in
    qemu*) printf 'COMM=%s CMDLINE=' "$comm"; tr '\000' ' ' < "$path"; printf '\n' ;;
  esac
done"""
    command_lines = k3s.kubectl(*compute, "sh", "-c", script)
    (output / "qemu-cmdline.txt").write_text(command_lines)
    arguments = [
        line.removeprefix("COMM=").split(" CMDLINE=", 1)[1]
        for line in command_lines.splitlines()
        if " CMDLINE=" in line
    ]
    if not arguments:
        raise RuntimeError(f"could not read a QEMU process command line from launcher {pod}")
    return domain_xml, command_lines


def _wait_for_delete(k3s: baseline.K3s, resource: str, name: str, namespace: str, timeout: int) -> bool:
    k3s.kubectl("wait", "--for=delete", f"{resource}/{name}", "-n", namespace, f"--timeout={timeout}s", check=False)
    return not k3s.kubectl(
        "get", resource, name, "-n", namespace, "--ignore-not-found=true", "-o", "name", check=False, stdout_only=True
    ).strip()


def _create_namespace(k3s: baseline.K3s, name: str) -> dict[str, Any]:
    try:
        return _create(k3s, _namespace(name))
    except Exception:
        # The API may have committed the create before the client lost its response. Recover its
        # UID only when the one-use label proves this randomly named namespace belongs to us.
        current = k3s.kubectl(
            "get", "namespace", name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
        )
        if current:
            namespace = json.loads(current)
            if namespace["metadata"].get("labels", {}).get(_OWNER_LABEL) == _owner_value(name):
                return cast(dict[str, Any], namespace)
        raise


def _create_namespaced(
    k3s: baseline.K3s, namespace: str, namespace_uid: str, resource: dict[str, Any]
) -> dict[str, Any]:
    """Recover a committed create if kubectl lost its response, only within our owned namespace."""
    try:
        return _create(k3s, resource)
    except Exception:
        current_namespace = k3s.kubectl(
            "get", "namespace", namespace, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
        )
        if not current_namespace:
            raise
        namespace_object = json.loads(current_namespace)
        namespace_metadata = namespace_object["metadata"]
        if namespace_metadata.get("uid") != namespace_uid or namespace_metadata.get("labels", {}).get(
            _OWNER_LABEL
        ) != _owner_value(namespace):
            raise RuntimeError(f"refusing create recovery in namespace {namespace}: its owner identity changed")

        kind = resource["kind"]
        resource_type = {"ConfigMap": "configmap", "PersistentVolumeClaim": "pvc", "VirtualMachineInstance": "vmi"}[
            kind
        ]
        name = resource["metadata"]["name"]
        current_resource = k3s.kubectl(
            "get",
            resource_type,
            name,
            "-n",
            namespace,
            "-o",
            "json",
            "--ignore-not-found=true",
            check=False,
            stdout_only=True,
        )
        if not current_resource:
            raise
        resource_object = json.loads(current_resource)
        metadata = resource_object["metadata"]
        if metadata.get("namespace") != namespace or metadata.get("labels", {}).get(_OWNER_LABEL) != _owner_value(
            namespace
        ):
            raise RuntimeError(f"refusing create recovery for unowned {kind} {name} in {namespace}")
        return cast(dict[str, Any], resource_object)


def _cleanup_namespace(
    k3s: baseline.K3s, name: str, uid: str, pvc_uid: str | None, vmi_uid: str | None, policy_uid: str | None
) -> None:
    namespace_json = k3s.kubectl(
        "get", "namespace", name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
    )
    if not namespace_json:
        return
    namespace = json.loads(namespace_json)
    if namespace["metadata"].get("uid") != uid or namespace["metadata"].get("labels", {}).get(
        _OWNER_LABEL
    ) != _owner_value(name):
        raise RuntimeError(f"refusing to delete namespace {name}: its UID or ownership label changed")

    vmi_json = k3s.kubectl(
        "get", "vmi", _VMI_NAME, "-n", name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
    )
    if vmi_json:
        vmi = json.loads(vmi_json)
        if vmi["metadata"].get("uid") != vmi_uid or vmi["metadata"].get("labels", {}).get(_OWNER_LABEL) != _owner_value(
            name
        ):
            raise RuntimeError(f"refusing to delete unowned VMI {_VMI_NAME} in {name}")
        k3s.kubectl("delete", "vmi", _VMI_NAME, "-n", name, "--wait=false", check=False)
        if not _wait_for_delete(k3s, "vmi", _VMI_NAME, name, _SHUTDOWN_WAIT_SECONDS):
            # Failure cleanup is scoped to this action-owned k3s cluster and namespace. The actual
            # experiment above always uses ordinary graceful VMI deletion first.
            k3s.kubectl(
                "delete", "vmi", _VMI_NAME, "-n", name, "--grace-period=0", "--force", "--wait=false", check=False
            )
            if not _wait_for_delete(k3s, "vmi", _VMI_NAME, name, 30):
                raise RuntimeError(f"action-owned VMI {_VMI_NAME} did not disappear during cleanup")

    policy_name = name + _POLICY_SUFFIX
    policy_json = k3s.kubectl(
        "get", "clusterpolicy", policy_name, "-o", "json", "--ignore-not-found=true", check=False, stdout_only=True
    )
    if policy_json:
        policy = json.loads(policy_json)
        metadata = policy["metadata"]
        if (
            metadata.get("uid") != policy_uid
            or metadata.get("labels", {}).get(_OWNER_LABEL) != _owner_value(name)
            or metadata.get("labels", {}).get(_OWNER_NAMESPACE_LABEL) != name
        ):
            raise RuntimeError(f"refusing to delete ClusterPolicy {policy_name}: its UID or ownership labels changed")
        k3s.kubectl("delete", "clusterpolicy", policy_name, "--wait=true", "--timeout=30s")
        if k3s.kubectl(
            "get", "clusterpolicy", policy_name, "--ignore-not-found=true", "-o", "name", check=False, stdout_only=True
        ).strip():
            raise RuntimeError(f"action-owned ClusterPolicy {policy_name} did not disappear")

    pvc_json = k3s.kubectl(
        "get",
        "pvc",
        _STATE_PVC_NAME,
        "-n",
        name,
        "-o",
        "json",
        "--ignore-not-found=true",
        check=False,
        stdout_only=True,
    )
    if pvc_json:
        pvc = json.loads(pvc_json)
        if pvc["metadata"].get("uid") != pvc_uid or pvc["metadata"].get("labels", {}).get(_OWNER_LABEL) != _owner_value(
            name
        ):
            raise RuntimeError(f"refusing to delete state PVC {_STATE_PVC_NAME}: its UID or ownership label changed")
        k3s.kubectl("delete", "pvc", _STATE_PVC_NAME, "-n", name, "--wait=true", "--timeout=60s", check=False)
        if k3s.kubectl(
            "get",
            "pvc",
            _STATE_PVC_NAME,
            "-n",
            name,
            "--ignore-not-found=true",
            "-o",
            "name",
            check=False,
            stdout_only=True,
        ).strip():
            raise RuntimeError(f"action-owned state PVC {_STATE_PVC_NAME} did not disappear")
    k3s.kubectl("delete", "namespace", name, "--wait=true", "--timeout=90s")
    if k3s.kubectl(
        "get", "namespace", name, "--ignore-not-found=true", "-o", "name", check=False, stdout_only=True
    ).strip():
        raise RuntimeError(f"action-owned namespace {name} survived deletion")
    if pvc_uid:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            volumes = json.loads(
                k3s.kubectl("get", "pv", "-o", "json", check=False, stdout_only=True) or '{"items":[]}'
            )["items"]
            if not any(volume.get("spec", {}).get("claimRef", {}).get("uid") == pvc_uid for volume in volumes):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"local-path PV for state PVC UID {pvc_uid} survived cleanup")


def _write_failure_report(k3s: baseline.K3s, namespace: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for filename, args in [
        ("cluster-report.txt", ("get", "pods,events", "-A", "-o", "wide")),
        ("namespace.yaml", ("get", "namespace", namespace, "-o", "yaml")),
        ("vmi.yaml", ("get", "vmi", _VMI_NAME, "-n", namespace, "-o", "yaml")),
        ("namespace-pods.yaml", ("get", "pods", "-n", namespace, "-o", "yaml")),
        ("namespace-events.yaml", ("get", "events", "-n", namespace, "--sort-by=.lastTimestamp", "-o", "yaml")),
        ("namespace-pvc.yaml", ("get", "pvc", "-n", namespace, "-o", "yaml")),
        ("namespace-configmaps.yaml", ("get", "configmap", "-n", namespace, "-o", "yaml")),
        ("namespace-launcher-policy.yaml", ("get", "clusterpolicy", namespace + _POLICY_SUFFIX, "-o", "yaml")),
        ("kubevirt.yaml", ("get", "kubevirt", "kubevirt", "-n", infrastructure.KUBEVIRT_NAMESPACE, "-o", "yaml")),
        ("node.yaml", ("get", "node", "env", "-o", "yaml")),
    ]:
        try:
            (output / filename).write_text(k3s.kubectl(*args, check=False))
        except Exception as error:
            (output / filename).write_text(f"report capture failed: {error!r}\n")
    try:
        (output / "k3s-report.txt").write_text(k3s.report(namespace))
    except Exception as error:
        (output / "k3s-report.txt").write_text(f"report capture failed: {error!r}\n")


def test_virtiofs_shutdown_policy_is_the_production_rule_scoped_to_one_namespace() -> None:
    namespace = _NAMESPACE_PREFIX + "unit-test"

    policy = _virtiofs_shutdown_policy(namespace)

    assert policy["metadata"]["name"] == namespace + _POLICY_SUFFIX
    assert policy["metadata"]["labels"][_OWNER_NAMESPACE_LABEL] == namespace
    rules = policy["spec"]["rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["name"] == "keep-virtiofs-through-guest-shutdown"
    resources = rule["match"]["any"][0]["resources"]
    assert resources["namespaces"] == [namespace]
    assert resources["selector"]["matchLabels"] == launcher_relay.LAUNCHER_LABELS
    assert [entry["name"] for entry in rule["context"]] == ["vmiOwner", "vmi"]
    foreach = rule["mutate"]["foreach"]
    assert len(foreach) == 1
    assert foreach[0]["list"] == "request.object.spec.containers"
    patches = foreach[0]["patchesJson6902"]
    assert "/spec/initContainers/-" in patches
    assert '"restartPolicy":"Always"' in patches
    assert "/spec/containers/{{elementIndex}}" in patches


def test_shutdown_policy_cleanup_after_generation_failure_ignores_missing_cluster_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _NAMESPACE_PREFIX + "unit-test"
    namespace_object = {
        "metadata": {"name": namespace, "uid": "owned-uid", "labels": {_OWNER_LABEL: _owner_value(namespace)}}
    }

    class FakeK3s:
        def __init__(self) -> None:
            self.namespace_exists = True
            self.calls: list[tuple[tuple[str, ...], bool]] = []

        def kubectl(self, *args: str, check: bool = True, stdout_only: bool = False) -> str:
            self.calls.append((args, stdout_only))
            if args[:2] == ("get", "namespace"):
                return json.dumps(namespace_object) if self.namespace_exists else ""
            if args[0] == "get" and args[1] in {"vmi", "clusterpolicy", "pvc"}:
                # kubectl can print a NotFound diagnostic on stderr even with --ignore-not-found;
                # stdout-only reads keep that diagnostic from being mistaken for JSON/object data.
                return "" if stdout_only else "Error from server (NotFound): resource not found"
            if args[:2] == ("delete", "namespace"):
                self.namespace_exists = False
                return ""
            raise AssertionError(f"unexpected kubectl call: {args}")

    k3s = FakeK3s()

    def fail_generation(_namespace: str) -> dict[str, Any]:
        raise RuntimeError("policy generation failed before API create")

    monkeypatch.setattr(sys.modules[__name__], "_virtiofs_shutdown_policy", fail_generation)
    with pytest.raises(RuntimeError, match="policy generation failed"):
        _create_cluster_policy(cast(baseline.K3s, k3s), namespace)

    _cleanup_namespace(cast(baseline.K3s, k3s), namespace, "owned-uid", None, None, None)

    assert any(args[:2] == ("delete", "namespace") for args, _ in k3s.calls)
    assert all(args[:2] != ("create", "-") for args, _ in k3s.calls)
    assert any(args[:2] == ("get", "clusterpolicy") and stdout_only for args, stdout_only in k3s.calls)


def test_virtiofs_policy_skip_is_opt_in_and_rejects_other_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SKIP_VIRTIOFS_POLICY_ENV, raising=False)
    assert _skip_virtiofs_shutdown_policy() is False

    monkeypatch.setenv(_SKIP_VIRTIOFS_POLICY_ENV, "1")
    assert _skip_virtiofs_shutdown_policy() is True

    monkeypatch.setenv(_SKIP_VIRTIOFS_POLICY_ENV, "0")
    assert _skip_virtiofs_shutdown_policy() is False

    monkeypatch.setenv(_SKIP_VIRTIOFS_POLICY_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _skip_virtiofs_shutdown_policy()


def test_shutdown_policy_cycle_count_and_debug_args_are_bounded_opt_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_CYCLES_ENV, raising=False)
    monkeypatch.delenv(_DEBUG_KERNEL_ARGS_ENV, raising=False)
    assert _cycle_count() == 1
    assert _debug_kernel_args() is False

    monkeypatch.setenv(_CYCLES_ENV, "3")
    monkeypatch.setenv(_DEBUG_KERNEL_ARGS_ENV, "1")
    assert _cycle_count() == 3
    assert _debug_kernel_args() is True

    for value in ("0", "4", "abc"):
        monkeypatch.setenv(_CYCLES_ENV, value)
        with pytest.raises(ValueError, match="integer from 1 to 3"):
            _cycle_count()
    monkeypatch.setenv(_DEBUG_KERNEL_ARGS_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _debug_kernel_args()


def test_shutdown_policy_debug_kernel_args_change_only_guest_boot_args() -> None:
    images = {
        "agentplane-vm-guest-boot:unset": "boot",
        "agentplane-vm-guest-root:unset": "root",
        "agentplane-runner-disk:unset": "runner",
    }
    namespace = _NAMESPACE_PREFIX + "unit-test"
    plain = _vmi(namespace, images)
    debug = _vmi(namespace, images, debug_kernel_args=True)
    plain_args = plain["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    debug_args = debug["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert debug_args == f"{plain_args} {_DEBUG_KERNEL_ARGS}"
    debug["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] = plain_args
    assert debug == plain


def test_boot_rcu_observer_is_opt_in_and_changes_only_guest_boot_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_BOOT_RCU_ENV, raising=False)
    assert _boot_rcu_observer() is False
    monkeypatch.setenv(_BOOT_RCU_ENV, "1")
    assert _boot_rcu_observer() is True
    monkeypatch.setenv(_BOOT_RCU_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _boot_rcu_observer()

    images = {
        "agentplane-vm-guest-boot:unset": "boot",
        "agentplane-vm-guest-root:unset": "root",
        "agentplane-runner-disk:unset": "runner",
    }
    plain = _vmi(_NAMESPACE_PREFIX + "unit-test", images)
    observed = _vmi(_NAMESPACE_PREFIX + "unit-test", images, boot_rcu_observe=True)
    plain_args = plain["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    observed_args = observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert observed_args == f"{plain_args} agentplane.boot-rcu-observe=1"
    observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] = plain_args
    assert observed == plain


def test_synthetic_slice_probe_is_opt_in_and_changes_only_guest_boot_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SYNTHETIC_SLICE_ENV, raising=False)
    assert _synthetic_slice_probe() is False
    monkeypatch.setenv(_SYNTHETIC_SLICE_ENV, "1")
    assert _synthetic_slice_probe() is True
    monkeypatch.setenv(_SYNTHETIC_SLICE_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _synthetic_slice_probe()

    images = {
        "agentplane-vm-guest-boot:unset": "boot",
        "agentplane-vm-guest-root:unset": "root",
        "agentplane-runner-disk:unset": "runner",
    }
    plain = _vmi(_NAMESPACE_PREFIX + "unit-test", images)
    observed = _vmi(_NAMESPACE_PREFIX + "unit-test", images, synthetic_slice_probe=True)
    plain_args = plain["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    observed_args = observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert observed_args == f"{plain_args} agentplane.synthetic-slice-probe=1"
    observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] = plain_args
    assert observed == plain


def test_synthetic_slice_success_requires_ordered_exact_guest_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_line = "AGENTPLANE_SYNTHETIC_SLICE_PHASE=baseline uptime=30.0 root_controllers=cpu,memory root_subtree_control= slice_present=no unit_state=inactive"
    active = "AGENTPLANE_SYNTHETIC_SLICE_PHASE=active uptime=31.0 root_controllers=cpu,memory root_subtree_control=cpu,memory slice_present=yes unit_state=active"
    after = "AGENTPLANE_SYNTHETIC_SLICE_PHASE=after uptime=40.0 root_controllers=cpu,memory root_subtree_control=cpu,memory slice_present=no unit_state=inactive"
    complete = "\n".join((baseline_line, active, after, _SYNTHETIC_SLICE_SUCCESS)) + "\n"
    assert _synthetic_slice_complete(complete)
    assert not _synthetic_slice_complete("\n".join((active, baseline_line, after, _SYNTHETIC_SLICE_SUCCESS)))
    assert not _synthetic_slice_complete(
        complete.replace(f"{_SYNTHETIC_SLICE_SUCCESS}\n", "noise" + _SYNTHETIC_SLICE_SUCCESS + "\n")
    )
    assert not _synthetic_slice_complete(complete.replace(f"{after}\n", ""))

    class FakeK3s:
        def __init__(self) -> None:
            self.calls = 0

        def kubectl(self, *_args: str, **_kwargs: Any) -> str:
            self.calls += 1
            return complete.replace(f"{_SYNTHETIC_SLICE_SUCCESS}\n", "") if self.calls == 1 else complete

    fake = FakeK3s()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    output = tmp_path / "probe-console.txt"
    _wait_for_synthetic_slice(cast(baseline.K3s, fake), "scope", "launcher", output)
    assert fake.calls == 2
    assert output.read_text() == complete


def test_cgroup_pilot_is_opt_in_and_changes_only_guest_boot_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_CGROUP_PILOT_ENV, raising=False)
    assert _cgroup_pilot() is False
    monkeypatch.setenv(_CGROUP_PILOT_ENV, "1")
    assert _cgroup_pilot() is True
    monkeypatch.setenv(_CGROUP_PILOT_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _cgroup_pilot()

    images = {
        "agentplane-vm-guest-boot:unset": "boot",
        "agentplane-vm-guest-root:unset": "root",
        "agentplane-runner-disk:unset": "runner",
    }
    plain = _vmi(_NAMESPACE_PREFIX + "unit-test", images)
    observed = _vmi(_NAMESPACE_PREFIX + "unit-test", images, cgroup_pilot=True)
    plain_args = plain["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    observed_args = observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert observed_args == f"{plain_args} agentplane.cgroup-pilot=1"
    observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] = plain_args
    assert observed == plain


def test_cgroup_pilot_requires_all_ordered_exact_results_before_acpi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [
        f"AGENTPLANE_CGROUP_PILOT_RESULT step={step} value={'yes' if step.endswith('_on') else 'no'} property_status=0 trace=unavailable hits=unavailable"
        for step in _CGROUP_PILOT_STEPS
    ]
    success = "AGENTPLANE_CGROUP_PILOT_SUCCESS transitions=4 trace=unavailable trace_hits_total=0"
    complete = "\n".join(("AGENTPLANE_CGROUP_PILOT_BEGIN pairs=2 direct_root_writes=none", *results, success)) + "\n"
    assert _cgroup_pilot_complete(complete)
    assert not _cgroup_pilot_complete("\n".join((results[1], results[0], *results[2:], success)))
    assert not _cgroup_pilot_complete(complete.replace(f"{results[2]}\n", ""))
    assert not _cgroup_pilot_complete(complete.replace(f"{success}\n", f"noise{success}\n"))
    assert not _cgroup_pilot_complete(complete + "AGENTPLANE_CGROUP_PILOT_UNEXERCISED step=pair2_off\n")

    class FakeK3s:
        def __init__(self) -> None:
            self.calls = 0

        def kubectl(self, *_args: str, **_kwargs: Any) -> str:
            self.calls += 1
            return complete.replace(f"{success}\n", "") if self.calls == 1 else complete

    fake = FakeK3s()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    output = tmp_path / "cgroup-pilot-console.txt"
    _wait_for_cgroup_pilot(cast(baseline.K3s, fake), "scope", "launcher", output)
    assert fake.calls == 2
    assert output.read_text() == complete


def test_cgroup_stress_is_opt_in_and_changes_only_guest_boot_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_CGROUP_STRESS_ENV, raising=False)
    assert _cgroup_stress() is False
    monkeypatch.setenv(_CGROUP_STRESS_ENV, "1")
    assert _cgroup_stress() is True
    monkeypatch.setenv(_CGROUP_STRESS_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _cgroup_stress()

    images = {
        "agentplane-vm-guest-boot:unset": "boot",
        "agentplane-vm-guest-root:unset": "root",
        "agentplane-runner-disk:unset": "runner",
    }
    plain = _vmi(_NAMESPACE_PREFIX + "unit-test", images)
    observed = _vmi(_NAMESPACE_PREFIX + "unit-test", images, cgroup_stress=True)
    plain_args = plain["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    observed_args = observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert observed_args == f"{plain_args} agentplane.cgroup-stress=1"
    observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] = plain_args
    assert observed == plain


def test_cgroup_stress_resident_condition_matches_guest_boot_arg_and_probe_script() -> None:
    guest_dir = Runfiles.CreateOrRaise().root() / "_main/agentplane/system_tests/vm/guest"
    script = (guest_dir / "cgroup_pilot_probe").read_text()
    images = {
        "agentplane-vm-guest-boot:unset": "boot",
        "agentplane-vm-guest-root:unset": "root",
        "agentplane-runner-disk:unset": "runner",
    }
    for mode, kernel_arg in (("pilot", "agentplane.cgroup-pilot=1"), ("stress", "agentplane.cgroup-stress=1")):
        vmi = _vmi(
            _NAMESPACE_PREFIX + "unit-test", images, cgroup_pilot=mode == "pilot", cgroup_stress=mode == "stress"
        )
        boot_args = vmi["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"].split()
        assert kernel_arg in boot_args
        branch = script.split(f"\n{mode})\n", 1)[1].split("\n\t;;", 1)[0]
        resident_name = next(
            line.strip().removeprefix("unit=") for line in branch.splitlines() if line.strip().startswith("unit=")
        )
        resident = (guest_dir / resident_name).read_text()
        assert f"ConditionKernelCommandLine={kernel_arg}" in resident.splitlines()
        assert "Slice=pilot.slice" in resident.splitlines()
        assert "ExecStart=/usr/bin/busybox sleep 120" in resident.splitlines()
        if mode == "stress":
            for name in ("agentplane-cgroup-stress-probe.service", "agentplane-cgroup-stress-probe.timer"):
                assert f"ConditionKernelCommandLine={kernel_arg}" in (guest_dir / name).read_text().splitlines()


def test_cgroup_stress_requires_first_witness_ten_alternating_masks_and_trace_off() -> None:
    prefix = "AGENTPLANE_CGROUP_STRESS"
    records = [f"{prefix}_BEGIN pairs=5 direct_root_writes=none active_budget_seconds=75"]
    for step in _CGROUP_STRESS_STEPS:
        on = step.endswith("_on")
        before = "memory,pids" if on else "io,memory,pids"
        after = "io,memory,pids" if on else "memory,pids"
        records.append(
            f"{prefix}_SNAPSHOT step={step}_before uptime=40 root={before} slice={before} resident=yes pid=497"
        )
        records.append(
            f"{prefix}_MASK_WAIT step={step} sample=0 uptime=41 property={'yes' if on else 'no'} "
            f"root={before} slice={before} resident=yes pid=497"
        )
        records.append(
            f"{prefix}_MASK_CONVERGED step={step} sample=1 uptime=42 property={'yes' if on else 'no'} "
            f"root={after} slice={after} resident=yes"
        )
        if step == "pair1_on":
            records.append(f"{prefix}_TRACE_INTEGRITY step=pair1_on total_lines=5000 ring_kib=1027 counters=clean")
            records.append(
                f"{prefix}_TRACE_CHAIN step=pair1_on status=proven tree=3 "
                "path=cgroup_subtree_control_write>cgroup_update_dfl_csses>percpu_down_write>rcu_sync_enter"
            )
        records.append(f"{prefix}_SNAPSHOT step={step}_after uptime=46 root={after} slice={after} resident=yes pid=497")
        records.append(
            f"{prefix}_RESULT step={step} value={'yes' if on else 'no'} "
            f"property_status=0 observed_property={'yes' if on else 'no'} trace=enabled"
        )
        if step == "pair1_on":
            records.append(f"{prefix}_TRACE_PHASE after=pair1_on tracing_on=0 current_tracer=nop")
    records.append(f"{prefix}_SUCCESS transitions=10 trace=enabled trace_hits_total=2")
    complete = "\n".join(records) + "\n"
    assert _cgroup_stress_complete(complete)
    assert not _cgroup_stress_complete(complete.replace(f"{prefix}_TRACE_CHAIN step=pair1_on status=proven", "noise"))
    assert not _cgroup_stress_complete(complete.replace("counters=clean", "counters=cpu0:overflow_or_drop"))
    assert not _cgroup_stress_complete(complete.replace("tracing_on=0", "tracing_on=1"))
    assert not _cgroup_stress_complete(complete.replace("current_tracer=nop", "current_tracer=function_graph"))
    assert not _cgroup_stress_complete(
        complete.replace("step=pair3_on_after uptime=46 root=io", "step=pair3_on_after uptime=46 root=memory")
    )
    assert not _cgroup_stress_complete(complete.replace(f"{prefix}_MASK_CONVERGED step=pair3_on", "noise"))
    assert not _cgroup_stress_complete(
        complete.replace("observed_property=no trace=enabled", "observed_property=yes trace=enabled")
    )
    assert not _cgroup_stress_complete(
        complete.replace(f"{prefix}_RESULT step=pair5_off", f"noise{prefix}_RESULT step=pair5_off")
    )
    assert not _cgroup_stress_complete(complete + f"{prefix}_TRACE_INTEGRITY step=pair2_on counters=clean\n")
    assert not _cgroup_stress_complete(complete + f"{prefix}_UNEXERCISED step=pair5_off reason=active_budget\n")


def _delegated_cgroup_stress_records() -> list[str]:
    prefix = "AGENTPLANE_DELEGATED_CGROUP_STRESS"
    writer_pid = "321"
    resident_pid = "654"
    records = [
        f"{prefix}_BEGIN pairs={_DELEGATED_CGROUP_STRESS_PAIRS} direct_root_writes=none "
        "delegated_parent=systemd_service_child max_active_seconds=75",
        f"{prefix}_READY service_parent_empty=yes manager_empty=yes io_available=yes "
        f"writer_in_driver=yes resident_in_payload=yes resident_pid={resident_pid}",
        f"{prefix}_TRACE_PROOF step=proof_on io=yes verified=yes writer_pid={writer_pid} pid_match=yes "
        "trace_integrity=clean trace_bytes=1024 trace_lines=20 status=proven tree=1 "
        "path=cgroup_subtree_control_write>cgroup_apply_control>cgroup_update_dfl_csses>"
        "percpu_down_write>rcu_sync_enter",
        f"{prefix}_TRACE_DISABLED tracing_on=0 current_tracer=nop before_stress=yes",
    ]
    for step in ("proof_reset_off", *_DELEGATED_CGROUP_STRESS_STEPS):
        io = "yes" if step.endswith("_on") else "no"
        records.append(
            f"{prefix}_RESULT step={step} io={io} verified=yes writer_pid={writer_pid} resident_pid={resident_pid}"
        )
    records.extend(
        (
            f"{prefix}_CLEANUP owned_children_removed=yes resident_alive=no trace_restored=yes",
            f"{prefix}_SUCCESS transitions={_DELEGATED_CGROUP_STRESS_PAIRS * 2} "
            "delegated_subtree_only=yes tracing_during_pairs=no",
        )
    )
    return records


def test_delegated_cgroup_stress_requires_owned_descendant_witness_and_clean_pairs() -> None:
    records = _delegated_cgroup_stress_records()
    complete = "\n".join(records) + "\n"
    assert _delegated_cgroup_stress_complete(complete)

    bad_pid = complete.replace("pid_match=yes", "pid_match=no")
    assert not _delegated_cgroup_stress_complete(bad_pid)

    bad_path = complete.replace(
        "cgroup_subtree_control_write>cgroup_apply_control>cgroup_update_dfl_csses>percpu_down_write>rcu_sync_enter",
        "rcu_sync_enter>cgroup_update_dfl_csses>percpu_down_write>cgroup_subtree_control_write",
    )
    assert not _delegated_cgroup_stress_complete(bad_path)

    missing_reset = complete.replace(f"{records[4]}\n", "")
    assert not _delegated_cgroup_stress_complete(missing_reset)

    out_of_order = records.copy()
    out_of_order[5], out_of_order[6] = out_of_order[6], out_of_order[5]
    assert not _delegated_cgroup_stress_complete("\n".join(out_of_order) + "\n")

    missing_cleanup = complete.replace(f"{records[-2]}\n", "")
    assert not _delegated_cgroup_stress_complete(missing_cleanup)
    assert not _delegated_cgroup_stress_complete(
        complete + "AGENTPLANE_DELEGATED_CGROUP_STRESS_UNEXERCISED reason=no_io\n"
    )


def test_delegated_cgroup_stress_is_opt_in_and_adds_only_its_guest_boot_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_DELEGATED_CGROUP_STRESS_ENV, raising=False)
    assert _delegated_cgroup_stress() is False
    monkeypatch.setenv(_DELEGATED_CGROUP_STRESS_ENV, "1")
    assert _delegated_cgroup_stress() is True
    monkeypatch.setenv(_DELEGATED_CGROUP_STRESS_ENV, "true")
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _delegated_cgroup_stress()

    images = {
        "agentplane-vm-guest-boot:unset": "boot",
        "agentplane-vm-guest-root:unset": "root",
        "agentplane-runner-disk:unset": "runner",
    }
    namespace = _NAMESPACE_PREFIX + "unit-test"
    plain = _vmi(namespace, images, boot_rcu_observe=True)
    observed = _vmi(namespace, images, boot_rcu_observe=True, delegated_cgroup_stress=True)
    plain_args = plain["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    observed_args = observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"]
    assert observed_args == f"{plain_args} agentplane.delegated-cgroup-stress=1"
    observed["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] = plain_args
    assert observed == plain


def test_shutdown_policy_lifecycle_sample_distinguishes_absent_from_api_error_and_replaced_pod() -> None:
    namespace = _NAMESPACE_PREFIX + "unit-test"

    class FakeK3s:
        def __init__(self, result: str = "", error: Exception | None = None) -> None:
            self.result = result
            self.error = error

        def kubectl(self, *_args: str, **kwargs: Any) -> str:
            assert kwargs["timeout"] == 10
            assert kwargs["stdout_only"] is True
            if self.error is not None:
                raise self.error
            return self.result

    assert _launcher_lifecycle_sample(cast(baseline.K3s, FakeK3s()), namespace, "launcher", "pod-uid") == {
        "pod_absent": True
    }

    with pytest.raises(RuntimeError, match="API unavailable"):
        _launcher_lifecycle_sample(
            cast(baseline.K3s, FakeK3s(error=RuntimeError("API unavailable"))), namespace, "launcher", "pod-uid"
        )

    replacement = json.dumps({"metadata": {"uid": "new-pod-uid"}, "status": {}})
    with pytest.raises(RuntimeError, match="Pod UID changed"):
        _launcher_lifecycle_sample(cast(baseline.K3s, FakeK3s(result=replacement)), namespace, "launcher", "pod-uid")

    finished_at = "2026-11-06T23:58:00Z"
    stopped = json.dumps(
        {
            "metadata": {"uid": "pod-uid", "deletionTimestamp": "2026-11-06T23:57:59Z"},
            "status": {
                "phase": "Running",
                "initContainerStatuses": [
                    {
                        "name": "virtiofs-state",
                        "ready": False,
                        "restartCount": 0,
                        "state": {"terminated": {"finishedAt": finished_at}},
                    }
                ],
            },
        }
    )
    sample = _launcher_lifecycle_sample(cast(baseline.K3s, FakeK3s(result=stopped)), namespace, "launcher", "pod-uid")
    assert sample["pod_uid"] == "pod-uid"
    assert sample["initContainerStatuses"][0]["state"]["terminated"]["finishedAt"] == finished_at


def _run_cycle(
    k3s: baseline.K3s,
    guest_image_manifest: dict[str, str],
    output: Path,
    *,
    skip_virtiofs_policy: bool,
    debug_kernel_args: bool,
    boot_rcu_observe: bool,
    synthetic_slice_probe: bool,
    cgroup_pilot: bool,
    cgroup_stress: bool,
    delegated_cgroup_stress: bool,
) -> None:
    """Run one isolated VMI lifecycle and keep its evidence before cleanup."""
    output.mkdir(parents=True, exist_ok=True)
    namespace_name = _NAMESPACE_PREFIX + uuid.uuid4().hex[:10]
    namespace_uid: str | None = None
    vmi_uid: str | None = None
    pvc_uid: str | None = None
    policy_uid: str | None = None
    primary_error: BaseException | None = None

    try:
        namespace = _create_namespace(k3s, namespace_name)
        namespace_uid = namespace["metadata"]["uid"]
        policy: dict[str, Any] | None = None
        if not skip_virtiofs_policy:
            policy = _create_cluster_policy(k3s, namespace_name)
            policy_uid = policy["metadata"]["uid"]
            (output / "virtiofs-shutdown-policy.yaml").write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
            k3s.kubectl(
                "wait",
                "--for=condition=Ready",
                f"clusterpolicy/{namespace_name + _POLICY_SUFFIX}",
                f"--timeout={infrastructure.READY_SECONDS}s",
            )
        (output / "virtiofs-policy-mode.json").write_text(
            json.dumps(
                {
                    "skip_virtiofs_shutdown_policy": skip_virtiofs_policy,
                    "policy_enabled": not skip_virtiofs_policy,
                    "policy_name": policy["metadata"]["name"] if policy else None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        for name in ("ci-guest-config", "ci-guest-launch", "ci-guest-trust"):
            _create_namespaced(k3s, namespace_name, namespace_uid, _placeholder_config_map(namespace_name, name))
        pvc = _create_namespaced(k3s, namespace_name, namespace_uid, _state_claim(namespace_name))
        pvc_uid = pvc["metadata"]["uid"]
        vmi = _vmi(
            namespace_name,
            guest_image_manifest,
            debug_kernel_args=debug_kernel_args,
            boot_rcu_observe=boot_rcu_observe,
            synthetic_slice_probe=synthetic_slice_probe,
            cgroup_pilot=cgroup_pilot,
            cgroup_stress=cgroup_stress,
            delegated_cgroup_stress=delegated_cgroup_stress,
        )
        (output / "vmi.json").write_text(json.dumps(vmi, indent=2, sort_keys=True))

        vmi_was_created = False
        with (
            guest_logs.capture(
                log_root=Path("/var/log/pods"),
                pod_root=Path("/var/lib/kubelet/pods"),
                storage_root=k3s.data_dir / "storage",
                namespace=namespace_name,
                kubevirt_namespace=infrastructure.KUBEVIRT_NAMESPACE,
                destination=output / "vm-artifacts",
                validate_shutdown=lambda: vmi_was_created,
                require_boot_evidence=True,
                record_shutdown_diagnostic=True,
            ),
            host_qemu.capture(k3s.kubectl, namespace_name, output / "vm-artifacts"),
            ExitStack() as boot_observer,
        ):
            created_vmi = _create_namespaced(k3s, namespace_name, namespace_uid, vmi)
            vmi_was_created = True
            vmi_uid = created_vmi["metadata"]["uid"]
            if boot_rcu_observe:
                boot_observer.enter_context(
                    boot_rcu.capture(k3s.kubectl, namespace_name, vmi_uid, output / "boot-rcu-host.jsonl")
                )
            shutdown_diagnostic.write_progress(output, "started", sandbox_uid=vmi_uid)
            pvc = _wait_for_pvc(k3s, namespace_name)
            k3s.kubectl(
                "wait",
                "--for=condition=Ready",
                f"vmi/{_VMI_NAME}",
                "-n",
                namespace_name,
                f"--timeout={_BOOT_WAIT_SECONDS}s",
            )
            pod = _launcher(k3s, namespace_name)
            pod_name = pod["metadata"]["name"]
            pod_uid = pod["metadata"]["uid"]
            (output / "cycle-identity.json").write_text(
                json.dumps(
                    {
                        "namespace": namespace_name,
                        "namespace_uid": namespace_uid,
                        "vmi_uid": vmi_uid,
                        "launcher_pod_uid": pod_uid,
                        "pvc_uid": pvc_uid,
                        "policy_uid": policy_uid,
                        "policy_enabled": not skip_virtiofs_policy,
                        "debug_kernel_args": debug_kernel_args,
                        "boot_rcu_observer": boot_rcu_observe,
                        "synthetic_slice_probe": synthetic_slice_probe,
                        "cgroup_pilot": cgroup_pilot,
                        "cgroup_stress": cgroup_stress,
                        "delegated_cgroup_stress": delegated_cgroup_stress,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            (output / "launcher-pod.json").write_text(json.dumps(pod, indent=2, sort_keys=True))
            init_containers = pod.get("spec", {}).get("initContainers", [])
            containers = pod.get("spec", {}).get("containers", [])
            state_daemon = [container for container in init_containers if container.get("name") == "virtiofs-state"]
            ordinary_state_daemon = [container for container in containers if container.get("name") == "virtiofs-state"]
            if skip_virtiofs_policy:
                if state_daemon or len(ordinary_state_daemon) != 1:
                    raise AssertionError(
                        "without the scoped virtiofs rule, virtiofs-state must remain one ordinary launcher container"
                    )
            else:
                if len(state_daemon) != 1 or state_daemon[0].get("restartPolicy") != "Always":
                    raise AssertionError(
                        "the namespace-scoped production virtiofs rule did not move virtiofs-state to a restartable init container"
                    )
                if ordinary_state_daemon:
                    raise AssertionError("virtiofs-state remained an ordinary launcher container after admission")
            (output / "virtiofs-native-sidecar-admission.json").write_text(
                json.dumps(
                    {
                        "policy_enabled": not skip_virtiofs_policy,
                        "policy_name": policy["metadata"]["name"] if policy else None,
                        "policy_uid": policy_uid,
                        "policy_namespace_match": namespace_name,
                        "launcher_pod_uid": pod_uid,
                        "virtiofs_state_in_init_containers": bool(state_daemon),
                        "restart_policy": state_daemon[0].get("restartPolicy") if state_daemon else None,
                        "virtiofs_state_in_containers": bool(ordinary_state_daemon),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            (output / "node.yaml").write_text(k3s.kubectl("get", "node", "env", "-o", "yaml"))
            (output / "vmi-running.yaml").write_text(
                k3s.kubectl("get", "vmi", _VMI_NAME, "-n", namespace_name, "-o", "yaml")
            )
            (output / "guest-boot-console.txt").write_text(_wait_for_guest_boot(k3s, namespace_name, pod_name))
            if synthetic_slice_probe:
                _wait_for_synthetic_slice(k3s, namespace_name, pod_name, output / "synthetic-slice-console.txt")
            if cgroup_pilot:
                _wait_for_cgroup_pilot(k3s, namespace_name, pod_name, output / "cgroup-pilot-console.txt")
            if cgroup_stress:
                _wait_for_cgroup_stress(k3s, namespace_name, pod_name, output / "cgroup-stress-console.txt")
            if delegated_cgroup_stress:
                _wait_for_delegated_cgroup_stress(
                    k3s, namespace_name, pod_name, output / "delegated-cgroup-stress-console.txt"
                )
            _, qemu_cmdline = _qemu_evidence(k3s, namespace_name, pod_name, output)
            (output / "qemu-pre-shutdown.txt").write_text(
                f"namespace_uid={namespace_uid}\nvmi_uid={vmi_uid}\npvc_uid={pvc['metadata']['uid']}\n"
                f"launcher_pod_uid={pod_uid}\nnode={pod['spec'].get('nodeName')}\n"
                f"/dev/kvm_present_in_compute={k3s.kubectl('-n', namespace_name, 'exec', pod_name, '-c', 'compute', '--', 'sh', '-c', 'test -e /dev/kvm && echo yes || echo no').strip()}\n"
                f"libvirt_domain_type=qemu\n{qemu_cmdline}"
            )

            shutdown_diagnostic.write_progress(output, "sandbox_ready", sandbox_uid=vmi_uid)

            # A normal VMI deletion asks KubeVirt to send the guest ACPI power button. No
            # QMP command or force-delete participates in the measured shutdown path. The
            # additional read-only observer samples only this Pod's status every 2 seconds.
            lifecycle = output / "launcher-lifecycle.jsonl"
            stop_lifecycle, lifecycle_thread = _start_launcher_lifecycle_capture(
                k3s, namespace_name, pod_name, pod_uid, lifecycle
            )
            try:
                request_started = datetime.now(UTC).isoformat()
                with lifecycle.open("a") as record_file:
                    record_file.write(
                        json.dumps(
                            {"event": "vmi_delete_request_started", "observed_at_utc": request_started}, sort_keys=True
                        )
                        + "\n"
                    )
                k3s.kubectl("delete", "vmi", _VMI_NAME, "-n", namespace_name, "--wait=false")
                with lifecycle.open("a") as record_file:
                    record_file.write(
                        json.dumps(
                            {
                                "event": "vmi_delete_request_returned",
                                "request_started_at_utc": request_started,
                                "observed_at_utc": datetime.now(UTC).isoformat(),
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                if not _wait_for_delete(k3s, "vmi", _VMI_NAME, namespace_name, _SHUTDOWN_WAIT_SECONDS):
                    raise RuntimeError("KubeVirt did not remove the VMI after the bounded graceful deletion")
                shutdown_diagnostic.write_progress(output, "sandbox_deleted", sandbox_uid=vmi_uid)
            finally:
                _stop_launcher_lifecycle_capture(stop_lifecycle, lifecycle_thread)

        request_count = _write_shutdown_request_count(output)
        if request_count < 1:
            raise RuntimeError("ordinary VMI deletion completed without any retained guest ACPI request marker")
        _require_shutdown_proof_progress(output / "vm-artifacts" / "consoles", output)

    except BaseException as error:
        primary_error = error
        _write_shutdown_request_count(output)
        _write_failure_report(k3s, namespace_name, output)
        raise
    finally:
        cleanup_errors: list[Exception] = []
        if namespace_uid is not None:
            try:
                current_vmi = k3s.kubectl(
                    "get", "vmi", _VMI_NAME, "-n", namespace_name, "-o", "json", "--ignore-not-found=true", check=False
                )
                if current_vmi:
                    obj = json.loads(current_vmi)
                    if obj["metadata"].get("uid") != vmi_uid or obj["metadata"].get("labels", {}).get(
                        _OWNER_LABEL
                    ) != _owner_value(namespace_name):
                        raise RuntimeError("refusing cleanup: the VMI identity or ownership label changed")
                _cleanup_namespace(k3s, namespace_name, namespace_uid, pvc_uid, vmi_uid, policy_uid)
            except Exception as error:
                cleanup_errors.append(error)
                _write_failure_report(k3s, namespace_name, output)
        if cleanup_errors:
            message = "action-owned namespace cleanup failed: " + "; ".join(repr(error) for error in cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                raise RuntimeError(message)


@pytest.mark.timeout(1200)
def test_ci_guest_shutdown_via_standalone_kubevirt_vmi() -> None:
    """Run bounded, UID-isolated VMI shutdown cycles in one k3s installation."""
    output = undeclared_outputs_dir() / "standalone-vmi-tcg"
    output.mkdir(parents=True, exist_ok=True)
    runfiles = Runfiles.CreateOrRaise()
    runfiles_root = runfiles.root()
    binary = runfiles_root / os.environ["K3S_BINARY_RLOCATION"]
    image_manifest = json.loads((runfiles_root / os.environ["K3S_IMAGE_MANIFEST_RLOCATION"]).read_text())
    vm_images = _layout_paths(runfiles, image_manifest["vm"])
    guest_images = _layout_paths(runfiles, image_manifest["guest"])
    all_images = vm_images | guest_images
    (output / "input-images.json").write_text(json.dumps(image_manifest, indent=2, sort_keys=True))
    cycle_count = _cycle_count()
    skip_virtiofs_policy = _skip_virtiofs_shutdown_policy()
    debug_kernel_args = _debug_kernel_args()
    boot_rcu_observe = _boot_rcu_observer()
    synthetic_slice_probe = _synthetic_slice_probe()
    cgroup_pilot = _cgroup_pilot()
    cgroup_stress = _cgroup_stress()
    delegated_cgroup_stress = _delegated_cgroup_stress()
    if synthetic_slice_probe and not boot_rcu_observe:
        raise ValueError(f"{_SYNTHETIC_SLICE_ENV}=1 requires {_BOOT_RCU_ENV}=1 for PID 1 and timer evidence")
    if cgroup_pilot and not boot_rcu_observe:
        raise ValueError(f"{_CGROUP_PILOT_ENV}=1 requires {_BOOT_RCU_ENV}=1 for PID 1 and timer evidence")
    if cgroup_pilot and synthetic_slice_probe:
        raise ValueError(f"{_CGROUP_PILOT_ENV}=1 cannot run with {_SYNTHETIC_SLICE_ENV}=1")
    if cgroup_stress and not boot_rcu_observe:
        raise ValueError(f"{_CGROUP_STRESS_ENV}=1 requires {_BOOT_RCU_ENV}=1 for PID 1 and timer evidence")
    if cgroup_stress and (cgroup_pilot or synthetic_slice_probe):
        raise ValueError(f"{_CGROUP_STRESS_ENV}=1 cannot run with {_CGROUP_PILOT_ENV}=1 or {_SYNTHETIC_SLICE_ENV}=1")
    if delegated_cgroup_stress and not boot_rcu_observe:
        raise ValueError(
            f"{_DELEGATED_CGROUP_STRESS_ENV}=1 requires {_BOOT_RCU_ENV}=1 for guest PID 1 and timer evidence"
        )
    if delegated_cgroup_stress and (cgroup_pilot or cgroup_stress or synthetic_slice_probe):
        raise ValueError(
            f"{_DELEGATED_CGROUP_STRESS_ENV}=1 cannot run with {_CGROUP_PILOT_ENV}=1, "
            f"{_CGROUP_STRESS_ENV}=1, or {_SYNTHETIC_SLICE_ENV}=1"
        )
    (output / "run-config.json").write_text(
        json.dumps(
            {
                "requested_cycles": cycle_count,
                "policy_enabled": not skip_virtiofs_policy,
                "debug_kernel_args": debug_kernel_args,
                "boot_rcu_observer": boot_rcu_observe,
                "synthetic_slice_probe": synthetic_slice_probe,
                "cgroup_pilot": cgroup_pilot,
                "cgroup_stress": cgroup_stress,
                "delegated_cgroup_stress": delegated_cgroup_stress,
                "debug_kernel_args_value": _DEBUG_KERNEL_ARGS if debug_kernel_args else "",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    work = Path(tempfile.mkdtemp(prefix="k3s-vmi-tcg-", dir=os.environ["TEST_TMPDIR"]))
    k3s = baseline.K3s(work, binary)
    process: subprocess.Popen[bytes] | None = None
    primary_error: BaseException | None = None
    try:
        baseline.stop_previous()
        process = k3s.start(layers_in_memory=False)
        k3s.import_images(all_images)
        releases = infrastructure.releases()
        infrastructure.install(k3s.kubectl, releases)
        infrastructure.wait_for_apis(k3s.kubectl)
        infrastructure.wait_until_ready(k3s.kubectl, require_cluster_policies=False)
        for cycle in range(1, cycle_count + 1):
            _run_cycle(
                k3s,
                image_manifest["guest"],
                output / "cycles" / f"{cycle:03d}",
                skip_virtiofs_policy=skip_virtiofs_policy,
                debug_kernel_args=debug_kernel_args,
                boot_rcu_observe=boot_rcu_observe,
                synthetic_slice_probe=synthetic_slice_probe,
                cgroup_pilot=cgroup_pilot,
                cgroup_stress=cgroup_stress,
                delegated_cgroup_stress=delegated_cgroup_stress,
            )
    except BaseException as error:
        primary_error = error
        if process is not None:
            try:
                (output / "k3s-report.txt").write_text(k3s.report("default"))
            except Exception as report_error:
                error.add_note(f"k3s report capture failed: {report_error!r}")
        raise
    finally:
        cleanup_error: Exception | None = None
        if process is not None:
            try:
                k3s.stop(process)
            except Exception as error:
                cleanup_error = error
        shutil.rmtree(work, ignore_errors=True)
        if cleanup_error is not None:
            if primary_error is not None:
                primary_error.add_note(f"action-owned k3s cleanup failed: {cleanup_error!r}")
            else:
                raise RuntimeError(f"action-owned k3s cleanup failed: {cleanup_error!r}") from cleanup_error


if __name__ == "__main__":
    pytest_bazel.main()
