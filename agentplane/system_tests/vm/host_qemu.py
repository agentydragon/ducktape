"""Collect host-side QEMU state while a VM's shutdown proof is pending."""

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

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


def _read_proc_field(output: TextIO, path: Path, field: str) -> None:
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
    output: TextIO, pod_uid: str, stop: threading.Event, deadline: float, *, proc_root: Path = Path("/proc")
) -> None:
    found = scanned = threads = 0
    uncertain = False
    stopped_reason: str | None = None
    if stop.is_set() or time.monotonic() >= deadline:
        output.write("HOST_VIRTIOFSD_SKIPPED budget-or-stop\n")
        return

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
        output.write(f"HOST_VIRTIOFSD_PID {process.name}\n")
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
def capture(kubectl: Kubectl, namespace: str, artifact_root: Path) -> Iterator[None]:
    """Collect at most two bounded host/QEMU samples while shutdown proof is pending."""
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="vm-host-qemu") as executor:
        observing = executor.submit(_observe, kubectl, namespace, artifact_root, stop)
        try:
            yield
        finally:
            stop.set()
            try:
                observing.result()
            except Exception as error:
                _record_collector_error(artifact_root / "host-qemu", error)
