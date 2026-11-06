"""Collect host-side QEMU state while a VM's shutdown proof is pending."""

import json
import logging
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
    return any(f"pod{candidate}" in cgroup for candidate in (pod_uid, pod_uid.replace("-", "_")))


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


def _snapshot_qemu(
    kubectl: Kubectl, namespace: str, pod: str, pod_uid: str, output: TextIO, stop: threading.Event
) -> None:
    deadline = time.monotonic() + SAMPLE_BUDGET_SECONDS
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
    kubectl: Kubectl, namespace: str, launcher: Path, artifact_root: Path, sample: int, stop: threading.Event
) -> None:
    pod_uid = _launcher_pod_uid(launcher, namespace)
    pod = launcher.name.removeprefix(f"{namespace}_").rsplit("_", 1)[0]
    directory = artifact_root / "host-qemu" / launcher.name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"shutdown-{sample}-{time.time_ns()}.txt"
    with path.open("w") as output:
        output.write(
            f"HOST_QEMU_SNAPSHOT phase=shutdown timestamp={datetime.now(UTC).isoformat(timespec='milliseconds')}\n"
            f"launcher={launcher.name} pod={pod} pod_uid={pod_uid}\n"
            f"request={SHUTDOWN_REQUESTED.decode()} proof_absent_after={REQUEST_GRACE_SECONDS:.1f}s\n"
            "QMP is read-only but may wake QEMU's main loop or vCPUs.\n"
        )
        output.flush()
        try:
            _snapshot_qemu(kubectl, namespace, pod, pod_uid, output, stop)
        except Exception as error:
            output.write(f"SNAPSHOT_ERROR {error!r}\n")
            output.flush()


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
