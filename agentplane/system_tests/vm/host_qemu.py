"""Collect host-side QEMU state while a VM's shutdown proof is pending."""

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO

from agentplane.system_tests.vm import guest_logs

logger = logging.getLogger(__name__)

SHUTDOWN_REQUESTED = b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED"
SHUTDOWN_COMPLETE = guest_logs.SHUTDOWN_COMPLETE

REQUEST_GRACE_SECONDS = 5.0
SAMPLE_GAP_SECONDS = 2.0
MAX_SAMPLES = 2
SAMPLE_BUDGET_SECONDS = 10.0
COMMAND_TIMEOUT_SECONDS = 2.0
VIRTIOFSD_BUDGET_SECONDS = 0.75
VIRTIOFSD_QEMU_RESERVE_SECONDS = 7.0
VIRTIOFSD_MAX_PROCESSES = 6
VIRTIOFSD_MAX_THREADS = 48
VIRTIOFSD_TIMELINE_ENV = "AGENTPLANE_VM_VIRTIOFSD_TIMELINE"
VIRTIOFSD_TIMELINE_INTERVAL_SECONDS = 2.0
VIRTIOFSD_TIMELINE_MAX_SAMPLES = 18
VIRTIOFSD_TIMELINE_MAX_SECONDS = 36.0
VIRTIOFSD_TIMELINE_WAIT_SECONDS = 600.0
VIRTIOFSD_TIMELINE_MAX_BYTES = 512 * 1024
VIRTIOFSD_TIMELINE_TARGET = "virtiofsd-timeline-target.json"
POLL_SECONDS = 0.1

LIBVIRT_URI = "qemu+unix:///session?socket=/var/run/libvirt/virtqemud-sock"
QEMU_PROBES = (
    ("query-status", None),
    ("query-cpus-fast", None),
    ("info registers -a", None),
    ("info lapic", 0),
    ("info lapic", 1),
    ("info pic", None),
    ("info irq", None),
)


Kubectl = Callable[..., str]


def virtiofsd_timeline_enabled() -> bool:
    """Return whether the explicit diagnostic run requested passive virtiofsd sampling."""
    return os.environ.get(VIRTIOFSD_TIMELINE_ENV) == "1"


def _exact_line(data: bytes, marker: bytes) -> bool:
    return any(
        line.endswith(b"\n") and line.removesuffix(b"\n").removesuffix(b"\r") == marker
        for line in data.splitlines(keepends=True)
    )


def _launcher_markers(launcher: Path, stop: threading.Event | None = None) -> tuple[bool, bool]:
    requested = complete = False
    for serial in launcher.glob("*/*.log"):
        if stop is not None and stop.is_set():
            break
        try:
            contents = serial.read_bytes()
        except FileNotFoundError:
            continue
        requested |= _exact_line(contents, SHUTDOWN_REQUESTED)
        complete |= _exact_line(contents, SHUTDOWN_COMPLETE)
        if requested and complete:
            break
    return requested, complete


def _namespace_launchers(serial_root: Path, namespace: str, stop: threading.Event | None = None) -> list[Path]:
    launchers: list[Path] = []
    for launcher in sorted(serial_root.glob(f"{namespace}_virt-launcher-*")):
        if stop is not None and stop.is_set():
            break
        if launcher.is_dir():
            launchers.append(launcher)
    return launchers


def _launcher_pod_uid(launcher: Path, namespace: str) -> str:
    prefix = f"{namespace}_virt-launcher-"
    if not launcher.name.startswith(prefix):
        raise ValueError(f"not a namespace-owned virt-launcher directory: {launcher.name}")
    pod_name, separator, pod_uid = launcher.name.removeprefix(prefix).rpartition("_")
    if not separator or not pod_name or not pod_uid:
        raise ValueError(f"virt-launcher directory has no Pod UID: {launcher.name}")
    return pod_uid


def _pod_uid_in_cgroup(cgroup: str, pod_uid: str) -> bool:
    # systemd slice names replace the Pod UID's hyphens with underscores.
    return any(
        re.search(rf"(?<![A-Za-z0-9_])pod{re.escape(candidate)}(?=$|[/.])", cgroup)
        for candidate in (pod_uid, pod_uid.replace("-", "_"))
    )


def virtiofs_container_names_by_id(pod_status: dict[str, object]) -> dict[str, str]:
    """Return sanitized names for virtiofs container IDs in either Pod status list."""
    result: dict[str, str] = {}
    for field in ("containerStatuses", "initContainerStatuses"):
        statuses = pod_status.get(field)
        if not isinstance(statuses, list):
            continue
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            container_id = status.get("containerID")
            if not isinstance(name, str) or not re.fullmatch(r"virtiofs-[a-z0-9][a-z0-9-]{0,62}", name):
                continue
            if not isinstance(container_id, str):
                continue
            match = re.fullmatch(r"[a-z][a-z0-9+.-]*://([a-fA-F0-9]{32,128})", container_id)
            if match is not None:
                result[match.group(1).lower()] = name
    return result


def _container_ids_in_cgroup(cgroup: str) -> set[str]:
    """Extract full hexadecimal runtime IDs without retaining unrelated cgroup paths."""
    return {match.lower() for match in re.findall(r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{32,128}(?![A-Fa-f0-9])", cgroup)}


class _TextWriter(Protocol):
    def write(self, value: str, /) -> int: ...

    def flush(self) -> None: ...


def _read_proc_field(output: _TextWriter, path: Path, field: str) -> None:
    try:
        value = path.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except OSError as error:
        output.write(f"{field.upper()}_ERROR {error!r}\n")
    else:
        output.write(f"{field.upper()}: {value}")
        if not value.endswith("\n"):
            output.write("\n")


def _snapshot_host_qemu(
    output: TextIO, pod_uid: str, stop: threading.Event, deadline: float, *, proc_root: Path = Path("/proc")
) -> None:
    found = scanned = 0
    for comm in proc_root.glob("[0-9]*/comm"):
        if stop.is_set() or time.monotonic() >= deadline:
            output.write("HOST_QEMU_PROCESS_SCAN_STOPPED\n")
            break
        process = comm.parent
        try:
            if not comm.read_text().startswith("qemu"):
                continue
            scanned += 1
            cgroup = process.joinpath("cgroup").read_text()
        except FileNotFoundError:
            continue
        except OSError as error:
            output.write(f"HOST_QEMU_PROCESS_ERROR pid={process.name} {error!r}\n")
            continue
        if not _pod_uid_in_cgroup(cgroup, pod_uid):
            continue

        found += 1
        output.write(f"HOST_QEMU_PID {process.name}\n")
        for field in ("cmdline", "cgroup", "status"):
            _read_proc_field(output, process / field, field)
        for task in sorted((process / "task").glob("[0-9]*")):
            if stop.is_set() or time.monotonic() >= deadline:
                output.write(f"HOST_QEMU_THREAD_SCAN_STOPPED pid={process.name}\n")
                return
            output.write(f"HOST_QEMU_HOST_TID {task.name}\n")
            for field in ("comm", "status", "stat", "wchan", "syscall", "stack", "schedstat"):
                _read_proc_field(output, task / field, field)
        output.flush()

    output.write(f"HOST_QEMU_PROCESS_SCAN scanned={scanned} matching_pod={found} pod_uid={pod_uid}\n")
    if found == 0:
        output.write(f"HOST_QEMU_PROCESS_MATCH_NONE pod_uid={pod_uid}\n")
    output.write("NSpid maps host PIDs/TIDs to nested namespace IDs; QMP thread IDs are recorded separately.\n")
    output.flush()


def _snapshot_host_virtiofsd(
    output: _TextWriter,
    pod_uid: str,
    stop: threading.Event,
    deadline: float,
    *,
    proc_root: Path = Path("/proc"),
    container_names_by_id: dict[str, str] | None = None,
) -> tuple[int, bool]:
    found = scanned = threads = 0
    uncertain = False
    stopped_reason: str | None = None
    if stop.is_set() or time.monotonic() >= deadline:
        output.write("HOST_VIRTIOFSD_SKIPPED budget-or-stop\n")
        return 0, False

    for comm in sorted(proc_root.glob("[0-9]*/comm"), key=lambda path: int(path.parent.name)):
        if stop.is_set() or time.monotonic() >= deadline:
            stopped_reason = "budget-or-stop"
            break
        if found >= VIRTIOFSD_MAX_PROCESSES:
            stopped_reason = "process-limit"
            break
        process = comm.parent
        try:
            if comm.read_text().strip() != "virtiofsd":
                continue
            scanned += 1
            cgroup = (process / "cgroup").read_text()
        except FileNotFoundError:
            continue
        except OSError as error:
            uncertain = True
            output.write(f"HOST_VIRTIOFSD_PROCESS_ERROR pid={process.name} type={type(error).__name__}\n")
            continue
        if not _pod_uid_in_cgroup(cgroup, pod_uid):
            continue

        found += 1
        container_ids = _container_ids_in_cgroup(cgroup)
        if len(container_ids) == 1:
            container_id = next(iter(container_ids))
            container_name = (container_names_by_id or {}).get(container_id, "unmapped")
            identity = f"container_id={container_id} container_name={container_name}"
        elif not container_ids:
            identity = "container_id=unavailable container_name=unmapped"
        else:
            identity = "container_id=ambiguous container_name=unmapped"
        output.write(f"HOST_VIRTIOFSD_PID {process.name} {identity}\n")
        for field in ("comm", "status", "wchan", "syscall", "stack"):
            if stop.is_set() or time.monotonic() >= deadline:
                stopped_reason = "budget-or-stop"
                break
            _read_proc_field(output, process / field, field)
        if stopped_reason is not None:
            break
        for task in (process / "task").glob("[0-9]*"):
            if stop.is_set() or time.monotonic() >= deadline:
                stopped_reason = "budget-or-stop"
                break
            if threads >= VIRTIOFSD_MAX_THREADS:
                stopped_reason = "thread-limit"
                break
            threads += 1
            output.write(f"HOST_VIRTIOFSD_HOST_TID {task.name}\n")
            for field in ("comm", "status", "wchan", "syscall", "stack"):
                if stop.is_set() or time.monotonic() >= deadline:
                    stopped_reason = "budget-or-stop"
                    break
                _read_proc_field(output, task / field, field)
            if stopped_reason is not None:
                break
        if stopped_reason is not None:
            break

    output.write(
        f"HOST_VIRTIOFSD_PROCESS_SCAN scanned={scanned} matching_pod={found} threads={threads} pod_uid={pod_uid}\n"
    )
    if stopped_reason is not None:
        output.write(f"HOST_VIRTIOFSD_SCAN_STOPPED reason={stopped_reason}\n")
    elif found == 0:
        output.write(f"HOST_VIRTIOFSD_PROCESS_{'MATCH_UNCERTAIN' if uncertain else 'MATCH_NONE'} pod_uid={pod_uid}\n")
    output.flush()
    return found, stopped_reason is None and not uncertain


class _BoundedTextWriter:
    """Cap one retained timeline sample, including its header and truncation marker."""

    _MARKER = "\nHOST_VIRTIOFSD_OUTPUT_TRUNCATED\n"

    def __init__(self, output: TextIO, max_bytes: int) -> None:
        self.output = output
        self.remaining = max_bytes - len(self._MARKER.encode())
        self.truncated = False

    def write(self, value: str) -> int:
        if self.truncated:
            return len(value)
        encoded = value.encode()
        if len(encoded) <= self.remaining:
            self.output.write(value)
            self.remaining -= len(encoded)
            return len(value)
        prefix = encoded[: max(self.remaining, 0)].decode(errors="ignore")
        if prefix:
            self.output.write(prefix)
        self.output.write(self._MARKER)
        self.remaining = 0
        self.truncated = True
        return len(value)

    def flush(self) -> None:
        self.output.flush()


def record_virtiofsd_ready_baseline(
    artifact_root: Path, namespace: str, pod: str, pod_uid: str, container_names_by_id: dict[str, str] | None = None
) -> bool:
    """Take one bounded pre-ACPI sample and arm later samples for this exact launcher Pod UID."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", namespace):
        raise ValueError(f"invalid Kubernetes namespace: {namespace!r}")
    if not re.fullmatch(r"virt-launcher-[a-z0-9][a-z0-9.-]*", pod):
        raise ValueError(f"invalid KubeVirt launcher Pod name: {pod!r}")
    if not re.fullmatch(r"[A-Za-z0-9-]+", pod_uid):
        raise ValueError(f"invalid Kubernetes Pod UID: {pod_uid!r}")

    launcher_name = f"{namespace}_{pod}_{pod_uid}"
    root = artifact_root / "host-qemu"
    directory = root / launcher_name
    target = root / VIRTIOFSD_TIMELINE_TARGET
    if target.exists():
        raise RuntimeError("the VM artifact set already has a virtiofsd timeline target")

    try:
        directory.mkdir(parents=True, exist_ok=True)
        sample_path = directory / f"virtiofsd-baseline-{time.time_ns()}.txt"
        with sample_path.open("w") as raw_output:
            output = _BoundedTextWriter(raw_output, VIRTIOFSD_TIMELINE_MAX_BYTES)
            output.write(
                f"HOST_VIRTIOFSD_SNAPSHOT phase=ready-baseline timestamp={datetime.now(UTC).isoformat(timespec='milliseconds')}\n"
                f"launcher={launcher_name} pod={pod} pod_uid={pod_uid}\n"
                "observer=local-proc-only qmp=disabled\n"
            )
            container_names_by_id = container_names_by_id or {}
            output.write(
                "HOST_VIRTIOFSD_CONTAINER_MAP "
                + json.dumps(
                    [
                        {"container_id": container_id, "container_name": container_name}
                        for container_id, container_name in sorted(container_names_by_id.items())
                    ],
                    sort_keys=True,
                )
                + "\n"
            )
            found, complete = _snapshot_host_virtiofsd(
                output,
                pod_uid,
                threading.Event(),
                time.monotonic() + VIRTIOFSD_BUDGET_SECONDS,
                container_names_by_id=container_names_by_id,
            )
            complete = complete and not output.truncated
            output.write(f"BASELINE_MATCHING_PROCESSES={found} BASELINE_SCAN_COMPLETE={str(complete).lower()}\n")
            output.flush()

        target_tmp = target.with_suffix(".tmp")
        target_tmp.write_text(
            json.dumps(
                {
                    "namespace": namespace,
                    "pod": pod,
                    "pod_uid": pod_uid,
                    "launcher": launcher_name,
                    "baseline": sample_path.name,
                    "container_names_by_id": container_names_by_id,
                },
                sort_keys=True,
            )
            + "\n"
        )
        target_tmp.replace(target)
        return True
    except Exception as error:
        _record_collector_error(root / launcher_name, error)
        return False


def _take_virtiofsd_timeline_sample(
    artifact_root: Path,
    launcher: Path,
    sample: int,
    stop: threading.Event,
    *,
    phase: str,
    container_names_by_id: dict[str, str] | None = None,
) -> tuple[int, bool]:
    namespace = launcher.name.split("_", 1)[0]
    pod_uid = _launcher_pod_uid(launcher, namespace)
    directory = artifact_root / "host-qemu" / launcher.name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"virtiofsd-{phase}-{sample:02d}-{time.time_ns()}.txt"
    with path.open("w") as raw_output:
        output = _BoundedTextWriter(raw_output, VIRTIOFSD_TIMELINE_MAX_BYTES)
        output.write(
            f"HOST_VIRTIOFSD_SNAPSHOT phase={phase} sample={sample} timestamp={datetime.now(UTC).isoformat(timespec='milliseconds')}\n"
            f"launcher={launcher.name} pod_uid={pod_uid}\n"
            "observer=local-proc-only qmp=disabled\n"
        )
        result = _snapshot_host_virtiofsd(
            output,
            pod_uid,
            stop,
            time.monotonic() + VIRTIOFSD_BUDGET_SECONDS,
            container_names_by_id=container_names_by_id,
        )
        result = (result[0], result[1] and not output.truncated)
        output.flush()
    return result


def _write_virtiofsd_timeline_summary(
    root: Path, *, launcher: str | None, pod_uid: str | None, sample_count: int, stop_reason: str, started: float
) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "virtiofsd-timeline-summary.txt").write_text(
            f"launcher={launcher or 'unknown'}\npod_uid={pod_uid or 'unknown'}\n"
            f"post_acpi_samples={sample_count}\nstop_reason={stop_reason}\n"
            f"elapsed_seconds={time.monotonic() - started:.3f}\n"
            "observer=local-proc-only qmp=disabled\n"
            "A matching process snapshot does not establish that virtiofsd was servicing guest requests.\n"
        )
    except OSError as error:
        _record_collector_error(root, error)


def _observe_virtiofsd_timeline(artifact_root: Path, namespace: str, stop: threading.Event) -> None:
    root = artifact_root / "host-qemu"
    target = root / VIRTIOFSD_TIMELINE_TARGET
    started = time.monotonic()
    target_deadline = started + VIRTIOFSD_TIMELINE_WAIT_SECONDS
    while not stop.is_set() and not target.exists() and time.monotonic() < target_deadline:
        stop.wait(min(POLL_SECONDS, target_deadline - time.monotonic()))
    if stop.is_set():
        _write_virtiofsd_timeline_summary(
            root,
            launcher=None,
            pod_uid=None,
            sample_count=0,
            stop_reason="capture-teardown-before-ready-baseline",
            started=started,
        )
        return
    if not target.exists():
        _write_virtiofsd_timeline_summary(
            root, launcher=None, pod_uid=None, sample_count=0, stop_reason="ready-baseline-timeout", started=started
        )
        return

    try:
        identity = json.loads(target.read_text())
        if not isinstance(identity, dict) or identity.get("namespace") != namespace:
            raise ValueError("timeline target namespace does not match the active test")
        pod = identity.get("pod")
        pod_uid = identity.get("pod_uid")
        container_names_by_id = identity.get("container_names_by_id", {})
        if not isinstance(pod, str) or not isinstance(pod_uid, str):
            raise ValueError("timeline target lacks launcher Pod identity")
        if not isinstance(container_names_by_id, dict) or any(
            not isinstance(container_id, str)
            or not re.fullmatch(r"[a-f0-9]{32,128}", container_id)
            or not isinstance(container_name, str)
            or not re.fullmatch(r"virtiofs-[a-z0-9][a-z0-9-]{0,62}", container_name)
            for container_id, container_name in container_names_by_id.items()
        ):
            raise ValueError("timeline target contains an invalid sanitized container map")
        launcher = artifact_root / "serial" / str(identity.get("launcher"))
        if not launcher.name.startswith(f"{namespace}_virt-launcher-"):
            raise ValueError("timeline launcher path does not match its namespace")
        if launcher.name != f"{namespace}_{pod}_{pod_uid}":
            raise ValueError("timeline launcher path does not match its Pod name")
    except Exception as error:
        _record_collector_error(root, error)
        return

    while not stop.is_set() and time.monotonic() < target_deadline and not launcher.is_dir():
        stop.wait(min(POLL_SECONDS, target_deadline - time.monotonic()))
    if stop.is_set():
        _write_virtiofsd_timeline_summary(
            root,
            launcher=launcher.name,
            pod_uid=pod_uid,
            sample_count=0,
            stop_reason="capture-teardown-before-serial",
            started=started,
        )
        return
    if not launcher.is_dir():
        _write_virtiofsd_timeline_summary(
            root,
            launcher=launcher.name,
            pod_uid=pod_uid,
            sample_count=0,
            stop_reason="serial-directory-timeout",
            started=started,
        )
        return
    try:
        if _launcher_pod_uid(launcher, namespace) != pod_uid:
            raise ValueError("timeline launcher path UID does not match its target")
    except Exception as error:
        _record_collector_error(root / launcher.name, error)
        return

    requested = False
    while not stop.is_set() and not requested and time.monotonic() < target_deadline:
        try:
            requested, _ = _launcher_markers(launcher, stop)
        except OSError as error:
            _record_collector_error(root / launcher.name, error)
            return
        if not requested:
            stop.wait(POLL_SECONDS)
    if stop.is_set():
        _write_virtiofsd_timeline_summary(
            root,
            launcher=launcher.name,
            pod_uid=pod_uid,
            sample_count=0,
            stop_reason="capture-teardown-before-acpi",
            started=started,
        )
        return
    if not requested:
        _write_virtiofsd_timeline_summary(
            root,
            launcher=launcher.name,
            pod_uid=pod_uid,
            sample_count=0,
            stop_reason="acpi-request-timeout",
            started=started,
        )
        return

    first_request = time.monotonic()
    next_sample = first_request
    sample_count = 0
    absent_after_proof = 0
    stop_reason = "sample-limit"
    while not stop.is_set() and sample_count < VIRTIOFSD_TIMELINE_MAX_SAMPLES:
        now = time.monotonic()
        if now - first_request >= VIRTIOFSD_TIMELINE_MAX_SECONDS:
            stop_reason = "time-limit"
            break
        if now < next_sample:
            stop.wait(min(POLL_SECONDS, next_sample - now))
            continue
        try:
            found, complete = _take_virtiofsd_timeline_sample(
                artifact_root,
                launcher,
                sample_count,
                stop,
                phase="shutdown",
                container_names_by_id=container_names_by_id,
            )
        except Exception as error:
            _record_collector_error(root / launcher.name, error)
            stop_reason = "sample-error"
            break
        sample_count += 1
        try:
            _, proof = _launcher_markers(launcher, stop)
        except OSError as error:
            _record_collector_error(root / launcher.name, error)
            stop_reason = "serial-read-error"
            break
        if proof and complete and found == 0:
            absent_after_proof += 1
            if absent_after_proof >= 2:
                stop_reason = "proof-and-daemon-absent"
                break
        else:
            absent_after_proof = 0
        next_sample += VIRTIOFSD_TIMELINE_INTERVAL_SECONDS

    if stop.is_set() and sample_count < VIRTIOFSD_TIMELINE_MAX_SAMPLES and stop_reason == "sample-limit":
        stop_reason = "capture-teardown"

    _write_virtiofsd_timeline_summary(
        root / launcher.name,
        launcher=launcher.name,
        pod_uid=pod_uid,
        sample_count=sample_count,
        stop_reason=stop_reason,
        started=first_request,
    )


def _remaining(deadline: float, stop: threading.Event) -> float | None:
    if stop.is_set():
        return None
    remaining = deadline - time.monotonic()
    return remaining if remaining > 0 else None


def _virsh(
    kubectl: Kubectl,
    namespace: str,
    pod: str,
    output: TextIO,
    stop: threading.Event,
    deadline: float,
    label: str,
    *arguments: str,
) -> str | None:
    remaining = _remaining(deadline, stop)
    if remaining is None:
        output.write(f"{label} SKIPPED budget-or-stop\n")
        output.flush()
        return None
    timeout = min(COMMAND_TIMEOUT_SECONDS, remaining)
    output.write(f"{datetime.now(UTC).isoformat(timespec='milliseconds')} {label} timeout={timeout:.3f}s\n")
    output.flush()
    try:
        return kubectl(
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            "compute",
            "--",
            "virsh",
            "-c",
            LIBVIRT_URI,
            *arguments,
            timeout=timeout,
            stdout_only=True,
        )
    except Exception as error:
        output.write(f"{label}_ERROR {error!r}\n")
        output.flush()
        return None


def _qemu_monitor_query(command: str, cpu_index: int | None) -> str:
    if command.startswith("info "):
        arguments: dict[str, object] = {"command-line": command}
        if cpu_index is not None:
            arguments["cpu-index"] = cpu_index
        return json.dumps({"execute": "human-monitor-command", "arguments": arguments})
    return json.dumps({"execute": command})


def _selected_fields(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in fields if field in value}


def _container_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        phase: _selected_fields(value[phase], fields)
        for phase, fields in (
            ("running", ("startedAt",)),
            ("terminated", ("exitCode", "signal", "reason", "startedAt", "finishedAt")),
            ("waiting", ("reason",)),
        )
        if phase in value
    }


def _launcher_container(name: object) -> bool:
    return isinstance(name, str) and (name == "compute" or name.startswith("virtiofs-"))


def _container_specs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    containers = []
    for container in value:
        if not isinstance(container, dict) or not _launcher_container(container.get("name")):
            continue
        resources = container.get("resources")
        selected = _selected_fields(container, ("name", "image", "imagePullPolicy", "restartPolicy"))
        if isinstance(resources, dict):
            selected["resources"] = {
                bound: _selected_fields(resources[bound], ("cpu", "memory", "ephemeral-storage"))
                for bound in ("requests", "limits")
                if bound in resources
            }
        containers.append(selected)
    return containers


def _container_statuses(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    statuses = []
    for container in value:
        if not isinstance(container, dict) or not _launcher_container(container.get("name")):
            continue
        selected = _selected_fields(container, ("name", "ready", "started", "restartCount"))
        selected["state"] = _container_state(container.get("state"))
        selected["lastState"] = _container_state(container.get("lastState"))
        statuses.append(selected)
    return statuses


def _pod_status_snapshot(payload: object, pod: str, pod_uid: str) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("name") != pod or metadata.get("uid") != pod_uid:
        return None
    spec = payload.get("spec")
    status = payload.get("status")
    if not isinstance(spec, dict) or not isinstance(status, dict):
        return None

    selected_metadata = _selected_fields(metadata, ("name", "uid", "creationTimestamp"))
    selected_metadata["deletionTimestamp"] = metadata.get("deletionTimestamp")
    return {
        "metadata": selected_metadata,
        "spec": {
            **_selected_fields(spec, ("nodeName", "terminationGracePeriodSeconds")),
            "containers": _container_specs(spec.get("containers")),
            "initContainers": _container_specs(spec.get("initContainers")),
        },
        "status": {
            **_selected_fields(status, ("phase", "startTime")),
            "containerStatuses": _container_statuses(status.get("containerStatuses")),
            "initContainerStatuses": _container_statuses(status.get("initContainerStatuses")),
        },
    }


def _snapshot_pod_status(
    kubectl: Kubectl, namespace: str, pod: str, pod_uid: str, output: TextIO, stop: threading.Event, deadline: float
) -> None:
    remaining = _remaining(deadline, stop)
    if remaining is None:
        output.write("POD_STATUS_SKIPPED budget-or-stop\n")
        return
    try:
        raw = kubectl(
            "-n",
            namespace,
            "get",
            "pod",
            pod,
            "-o",
            "json",
            timeout=min(COMMAND_TIMEOUT_SECONDS, remaining),
            stdout_only=True,
        )
        payload = json.loads(raw)
        snapshot = _pod_status_snapshot(payload, pod, pod_uid)
    except Exception as error:
        output.write(f"POD_STATUS_ERROR type={type(error).__name__}\n")
        return
    if snapshot is None:
        output.write("POD_STATUS_UID_OR_SHAPE_MISMATCH\n")
        return
    output.write(f"POD_STATUS {json.dumps(snapshot, sort_keys=True)}\n")
    output.flush()


def _snapshot_qemu(
    kubectl: Kubectl,
    namespace: str,
    pod: str,
    pod_uid: str,
    output: TextIO,
    stop: threading.Event,
    *,
    include_pod_status: bool,
) -> None:
    deadline = time.monotonic() + SAMPLE_BUDGET_SECONDS
    if include_pod_status:
        _snapshot_pod_status(kubectl, namespace, pod, pod_uid, output, stop, deadline)
        _snapshot_host_virtiofsd(
            output,
            pod_uid,
            stop,
            min(time.monotonic() + VIRTIOFSD_BUDGET_SECONDS, deadline - VIRTIOFSD_QEMU_RESERVE_SECONDS),
        )
    _snapshot_host_qemu(output, pod_uid, stop, deadline)
    if _remaining(deadline, stop) is None:
        output.write("VIRSH_SKIPPED host-process-snapshot-used-budget-or-stop\n")
        return

    names = _virsh(kubectl, namespace, pod, output, stop, deadline, "VIRSH_DOMAIN_LIST", "list", "--name")
    if names is None:
        return
    domains = names.split()
    output.write(f"VIRSH_DOMAINS {domains!r}\n")
    if len(domains) != 1:
        output.write(f"QEMU_PROBES_SKIPPED expected_one_domain found={len(domains)}\n")
        return

    domain = domains[0]
    for command, cpu_index in QEMU_PROBES:
        if _remaining(deadline, stop) is None:
            output.write("QEMU_PROBES_STOPPED budget-or-stop\n")
            return
        query = _qemu_monitor_query(command, cpu_index)
        label = f"QEMU_PROBE {command} cpu={cpu_index}"
        result = _virsh(kubectl, namespace, pod, output, stop, deadline, label, "qemu-monitor-command", domain, query)
        if result is not None:
            output.write(f"QEMU_RESULT {command} cpu={cpu_index}\n{result}\n")
            output.flush()


def _record_collector_error(root: Path, error: Exception) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "collector-errors.txt").open("a") as output:
            output.write(f"{datetime.now(UTC).isoformat(timespec='milliseconds')} COLLECTOR_ERROR {error!r}\n")
    except OSError:
        logger.exception("Could not retain VM host-QEMU collector error")


def _take_sample(
    kubectl: Kubectl,
    namespace: str,
    launcher: Path,
    artifact_root: Path,
    sample: int,
    stop: threading.Event,
    *,
    phase: str = "shutdown",
) -> None:
    pod_uid = _launcher_pod_uid(launcher, namespace)
    pod = launcher.name.removeprefix(f"{namespace}_").rsplit("_", 1)[0]
    directory = artifact_root / "host-qemu" / launcher.name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{phase}-{sample}-{time.time_ns()}.txt"
    with path.open("w") as output:
        output.write(
            f"HOST_QEMU_SNAPSHOT phase={phase} timestamp={datetime.now(UTC).isoformat(timespec='milliseconds')}\n"
            f"launcher={launcher.name} pod={pod} pod_uid={pod_uid}\n"
            "QMP is read-only but may wake QEMU's main loop or vCPUs.\n"
        )
        if phase == "shutdown":
            output.write(f"request={SHUTDOWN_REQUESTED.decode()} proof_absent_after={REQUEST_GRACE_SECONDS:.1f}s\n")
        output.flush()
        try:
            _snapshot_qemu(kubectl, namespace, pod, pod_uid, output, stop, include_pod_status=phase == "shutdown")
        except Exception as error:
            output.write(f"SNAPSHOT_ERROR {error!r}\n")
            output.flush()


def snapshot_pending(kubectl: Kubectl, namespace: str, artifact_root: Path) -> None:
    """Retain end-of-case evidence when teardown is blocked before ACPI can be sent."""
    stop = threading.Event()
    try:
        launchers = _namespace_launchers(artifact_root / "serial", namespace)
    except OSError as error:
        _record_collector_error(artifact_root / "host-qemu", error)
        return
    for launcher in launchers:
        try:
            if not _launcher_markers(launcher)[1]:
                _take_sample(kubectl, namespace, launcher, artifact_root, 0, stop, phase="report")
        except Exception as error:
            _record_collector_error(artifact_root / "host-qemu" / launcher.name, error)


def _observe(kubectl: Kubectl, namespace: str, artifact_root: Path, stop: threading.Event) -> None:
    serial_root = artifact_root / "serial"
    observed_at: dict[Path, float] = {}
    samples: dict[Path, int] = {}
    next_sample_at: dict[Path, float] = {}
    completed: set[Path] = set()

    while not stop.is_set():
        for launcher in _namespace_launchers(serial_root, namespace, stop):
            if stop.is_set():
                return
            if launcher in completed:
                continue
            try:
                requested, proof = _launcher_markers(launcher, stop)
            except OSError as error:
                _record_collector_error(artifact_root / "host-qemu" / launcher.name, error)
                completed.add(launcher)
                continue
            if proof:
                completed.add(launcher)
                continue
            if not requested:
                continue

            now = time.monotonic()
            first_request = observed_at.setdefault(launcher, now)
            count = samples.get(launcher, 0)
            due = now >= (first_request + REQUEST_GRACE_SECONDS if count == 0 else next_sample_at[launcher])
            if not due or count >= MAX_SAMPLES:
                if count >= MAX_SAMPLES:
                    completed.add(launcher)
                continue

            if stop.is_set():
                return
            # A guest may have written its proof since the marker scan above.
            try:
                if _launcher_markers(launcher, stop)[1]:
                    completed.add(launcher)
                    continue
            except OSError as error:
                _record_collector_error(artifact_root / "host-qemu" / launcher.name, error)
                completed.add(launcher)
                continue
            samples[launcher] = count + 1
            try:
                _take_sample(kubectl, namespace, launcher, artifact_root, count, stop)
            except Exception as error:
                _record_collector_error(artifact_root / "host-qemu" / launcher.name, error)
                completed.add(launcher)
                continue
            if count + 1 < MAX_SAMPLES:
                next_sample_at[launcher] = time.monotonic() + SAMPLE_GAP_SECONDS
            else:
                completed.add(launcher)
        stop.wait(POLL_SECONDS)


@contextmanager
def capture(
    kubectl: Kubectl, namespace: str, artifact_root: Path, *, passive_virtiofsd_timeline: bool = False
) -> Iterator[None]:
    """Collect existing QMP snapshots or an explicitly selected passive virtiofsd timeline."""
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="vm-host-qemu") as executor:
        if passive_virtiofsd_timeline:
            observing = executor.submit(_observe_virtiofsd_timeline, artifact_root, namespace, stop)
        else:
            observing = executor.submit(_observe, kubectl, namespace, artifact_root, stop)
        try:
            yield
        finally:
            stop.set()
            try:
                observing.result()
            except Exception as error:
                _record_collector_error(artifact_root / "host-qemu", error)
