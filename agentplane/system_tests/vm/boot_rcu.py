"""Opt-in, passive host-side boot samples for CI and the manual TCG VMI."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# gazelle:include_dep @pypi//protobuf
from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.vm import host_qemu

INTERVAL_SECONDS = 1.0
MAX_SECONDS = 150.0
MAX_SAMPLES = 120
MAX_BYTES = 256 * 1024
MAX_SUMMARY_BYTES = 1024
MAX_THREADS = 16
MAX_PROCESSES_SCANNED = 4096
MAX_SCAN_SECONDS = 0.5
POD_QUERY_TIMEOUT_SECONDS = 1.5
FULL_SUITE_INTERVAL_SECONDS = 1.0
FULL_SUITE_WINDOW_SECONDS = 360.0
FULL_SUITE_PRE_LAUNCH_SECONDS = 300.0
FULL_SUITE_MAX_SAMPLES = 360
FULL_SUITE_MAX_BYTES = 4 * 1024 * 1024
FULL_SUITE_MAX_LAUNCHERS = 8
FULL_SUITE_SUMMARY_BYTES = 16 * 1024
FULL_SUITE_MAX_SERIAL_FILES = 16
FULL_SUITE_MAX_SERIAL_BYTES = 32 * 1024 * 1024
SANDBOX_LABEL = "agentplane.allegedly.works/sandbox"


def _launcher_pod(kubectl: Callable[..., str], namespace: str, vmi_uid: str) -> tuple[str, str] | None:
    raw = kubectl(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        "kubevirt.io=virt-launcher,kubevirt.io/domain=ci-guest",
        "-o",
        "json",
        timeout=POD_QUERY_TIMEOUT_SECONDS,
        stdout_only=True,
    )
    matches = []
    for pod in json.loads(raw).get("items", []):
        metadata = pod.get("metadata", {})
        if any(owner.get("uid") == vmi_uid for owner in metadata.get("ownerReferences", [])):
            matches.append((metadata.get("name"), metadata.get("uid")))
    if len(matches) > 1:
        raise RuntimeError("multiple launcher Pods claim the diagnostic VMI UID")
    if not matches:
        return None
    name, uid = matches[0]
    if not isinstance(name, str) or not isinstance(uid, str):
        raise RuntimeError("launcher Pod has no name or UID")
    return name, uid


def _thread_state(path: Path) -> dict[str, object]:
    stat = (path / "stat").read_text()
    fields = stat[stat.rfind(")") + 2 :].split()
    schedstat = (path / "schedstat").read_text().split()
    return {
        "tid": int(path.name),
        "comm": (path / "comm").read_text().strip(),
        "state": fields[0],
        "utime_ticks": int(fields[11]),
        "stime_ticks": int(fields[12]),
        "sched_runtime_ns": int(schedstat[0]),
        "sched_wait_ns": int(schedstat[1]),
        "wchan": (path / "wchan").read_text().strip()[:128],
    }


def _sample_qemu(proc_root: Path, pod_uid: str) -> dict[str, object]:
    matches = []
    scanned = 0
    scan_deadline = time.monotonic() + MAX_SCAN_SECONDS
    truncated = False
    incomplete = False
    for comm in proc_root.glob("[0-9]*/comm"):
        if scanned >= MAX_PROCESSES_SCANNED or time.monotonic() >= scan_deadline:
            truncated = True
            break
        scanned += 1
        process = comm.parent
        try:
            if not comm.read_text().startswith("qemu"):
                continue
            if host_qemu._pod_uid_in_cgroup((process / "cgroup").read_text(), pod_uid):
                matches.append(process)
        except FileNotFoundError, ProcessLookupError:
            continue
        except OSError:
            incomplete = True
            continue
    if len(matches) != 1:
        return {
            "matching_qemu_processes": len(matches),
            "process_scan_truncated": truncated,
            "process_scan_incomplete": incomplete or truncated,
        }
    process = matches[0]
    tasks = sorted((process / "task").glob("[0-9]*"), key=lambda path: int(path.name))
    states = []
    for task in tasks[:MAX_THREADS]:
        try:
            states.append(_thread_state(task))
        except OSError as error:
            states.append({"tid": int(task.name), "error": type(error).__name__})
    return {
        "matching_qemu_processes": 1,
        "pid": int(process.name),
        "threads": states,
        "thread_count": len(tasks),
        "threads_truncated": len(tasks) > MAX_THREADS,
        "process_scan_truncated": truncated,
        "process_scan_incomplete": incomplete or truncated,
    }


def capture_readiness_failure(view: environment_service_pb2.EnvironmentDescriptor, artifact_root: Path) -> Path:
    """Write one passive, exact-Pod-UID QEMU snapshot for a VM that missed readiness."""
    target = view.target
    if not target.namespace or not target.name or not re.fullmatch(r"[A-Za-z0-9-]+", target.uid):
        raise ValueError("VM readiness snapshot requires a complete Sandbox identity")

    pod_identity: dict[str, str] | None = None
    host_state: dict[str, object] = {"not_sampled": "launcher Pod identity unavailable"}
    capture_status = "skipped_no_launcher_identity"
    if view.kubernetes_diagnostics.HasField("pod"):
        pod = view.kubernetes_diagnostics.pod
        pod_identity = {"namespace": pod.namespace, "name": pod.name, "uid": pod.uid}
        if pod.namespace == target.namespace and pod.name and re.fullmatch(r"[A-Za-z0-9-]+", pod.uid):
            try:
                host_state = _sample_qemu(Path("/proc"), pod.uid)
                capture_status = "sampled"
            except Exception as error:
                host_state = {"capture_error": type(error).__name__, "message": str(error)[:512]}
                capture_status = "collector_error"
        else:
            capture_status = "skipped_pod_identity_mismatch"
            host_state = {"not_sampled": "launcher Pod identity is incomplete or in another namespace"}

    record = {
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "capture_status": capture_status,
        "sandbox": {"namespace": target.namespace, "name": target.name, "uid": target.uid},
        "launcher_pod": pod_identity,
        "host_qemu": host_state,
    }
    encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError(f"VM readiness QEMU snapshot exceeded {MAX_BYTES} bytes")

    destination = artifact_root / "readiness-failures" / f"{target.uid}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    temporary.write_bytes(encoded)
    temporary.replace(destination)
    return destination


def _append_bounded(path: Path, record: dict[str, object], *, max_bytes: int | None = None) -> bool:
    encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
    if path.stat().st_size + len(encoded) > (MAX_BYTES if max_bytes is None else max_bytes):
        return False
    with path.open("ab") as output:
        output.write(encoded)
    return True


def _increment_integer_field(fields: dict[str, object], name: str, increment: int = 1) -> None:
    """Increment a JSON summary counter after validating its current value."""
    current = fields[name]
    if not isinstance(current, int):
        raise TypeError(f"summary field {name!r} is not an integer")
    fields[name] = current + increment


def _namespace_launchers(kubectl: Callable[..., str], namespace: str) -> tuple[list[dict[str, str]], int]:
    """Resolve only current sandbox launcher identities from one namespace Pod snapshot.

    A Pod UID is the cgroup identity used for host /proc attribution. The VMI owner UID and
    KubeVirt's created-by/id labels must agree before that Pod can enter the sample set.
    """
    raw = kubectl(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        "kubevirt.io=virt-launcher",
        "-o",
        "json",
        timeout=POD_QUERY_TIMEOUT_SECONDS,
        stdout_only=True,
    )
    payload = json.loads(raw)
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Pod list response has no items array")

    result: list[dict[str, str]] = []
    rejected = 0
    for pod in items:
        metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
        labels = metadata.get("labels", {})
        owners = metadata.get("ownerReferences", [])
        vmi_owners = [owner for owner in owners if owner.get("kind") == "VirtualMachineInstance"]
        if (
            metadata.get("namespace") != namespace
            or not isinstance(metadata.get("name"), str)
            or not isinstance(metadata.get("uid"), str)
            or not labels.get(SANDBOX_LABEL)
            or len(vmi_owners) != 1
        ):
            rejected += 1
            continue
        owner = vmi_owners[0]
        vmi_uid = owner.get("uid")
        vmi_name = owner.get("name")
        if (
            not isinstance(vmi_uid, str)
            or not isinstance(vmi_name, str)
            or labels.get("kubevirt.io/created-by") != vmi_uid
            or labels.get("vmi.kubevirt.io/id") != vmi_name
        ):
            rejected += 1
            continue
        result.append(
            {
                "pod_name": metadata["name"],
                "pod_uid": metadata["uid"],
                "vmi_name": vmi_name,
                "vmi_uid": vmi_uid,
                "phase": str(pod.get("status", {}).get("phase", "unknown")),
                "deletion_timestamp": str(metadata.get("deletionTimestamp", "")),
            }
        )
    result.sort(key=lambda launcher: launcher["pod_uid"])
    return result, rejected


def _sample_qemus(proc_root: Path, pod_uids: list[str]) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Scan /proc once and attribute each QEMU process to the exact current launcher Pod UID."""
    if not pod_uids:
        return {}, {"process_scan": "not-run-no-current-launchers"}
    matches: dict[str, list[Path]] = {pod_uid: [] for pod_uid in pod_uids}
    scanned = 0
    scan_deadline = time.monotonic() + MAX_SCAN_SECONDS
    truncated = False
    incomplete = False
    for comm in proc_root.glob("[0-9]*/comm"):
        if scanned >= MAX_PROCESSES_SCANNED or time.monotonic() >= scan_deadline:
            truncated = True
            break
        scanned += 1
        process = comm.parent
        try:
            if not comm.read_text().startswith("qemu"):
                continue
            cgroup = (process / "cgroup").read_text()
        except FileNotFoundError, ProcessLookupError:
            continue
        except OSError:
            incomplete = True
            continue
        for pod_uid in pod_uids:
            if host_qemu._pod_uid_in_cgroup(cgroup, pod_uid):
                matches[pod_uid].append(process)

    scan_status: dict[str, object] = {
        "processes_scanned": scanned,
        "process_scan_truncated": truncated,
        "process_scan_incomplete": incomplete or truncated,
    }
    result: dict[str, dict[str, object]] = {}
    for pod_uid, processes in matches.items():
        process_status: dict[str, object] = {"matching_qemu_processes": len(processes), **scan_status}
        if len(processes) == 1:
            process = processes[0]
            tasks = sorted((process / "task").glob("[0-9]*"), key=lambda path: int(path.name))
            states = []
            for task in tasks[:MAX_THREADS]:
                try:
                    states.append(_thread_state(task))
                except OSError as error:
                    states.append({"tid": int(task.name), "error": type(error).__name__})
            process_status.update(
                {
                    "pid": int(process.name),
                    "threads": states,
                    "thread_count": len(tasks),
                    "threads_truncated": len(tasks) > MAX_THREADS,
                }
            )
        result[pod_uid] = process_status
    return result, scan_status


def _write_full_suite_summary(path: Path, summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(summary, sort_keys=True) + "\n").encode()
    if len(encoded) > FULL_SUITE_SUMMARY_BYTES:
        raise ValueError("full-suite boot RCU summary exceeded its byte bound")
    temporary = path.with_name(path.name + ".partial")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def record_guest_summary_failure(artifact_root: Path, error: Exception) -> Path:
    """Record bounded failure metadata without copying exception text into artifacts."""
    destination = artifact_root / "boot-rcu-guest-summary-error.json"
    _write_full_suite_summary(
        destination,
        {
            "status": "failed",
            "error_type": type(error).__name__,
            "observed_at_utc": datetime.now(UTC).isoformat(),
            "host_summary": "full-suite-summary.json",
        },
    )
    return destination


def start_namespace(
    kubectl: Callable[..., str], namespace: str, destination: Path, *, proc_root: Path = Path("/proc")
) -> tuple[threading.Event, threading.Thread]:
    """Sample all sandbox VMs in one test namespace before readiness through bounded cleanup."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"")
    summary_path = destination.with_name(destination.stem + "-summary.json")
    _write_full_suite_summary(
        summary_path,
        {
            "status": "running",
            "window_seconds": FULL_SUITE_WINDOW_SECONDS,
            "pre_launch_wait_seconds": FULL_SUITE_PRE_LAUNCH_SECONDS,
            "sample_limit": FULL_SUITE_MAX_SAMPLES,
            "samples_written": 0,
            "stop_reason": "not_finished",
        },
    )
    stop = threading.Event()

    def collect() -> None:
        started = time.monotonic()
        pre_launch_deadline = started + FULL_SUITE_PRE_LAUNCH_SECONDS
        started_at = datetime.now(UTC).isoformat()
        first_launcher_at: float | None = None
        first_launcher_at_utc: str | None = None
        sampling_deadline: float | None = None
        pre_launch_polls = 0
        pre_launch_query_failures = 0
        sample_count = query_failures = rejected_pods = 0
        launcher_list_truncated = False
        stop_reason = "collector_error"
        clean_exit = False
        pod_stats: dict[str, dict[str, object]] = {}
        try:
            while True:
                now = time.monotonic()
                if first_launcher_at is None and now >= pre_launch_deadline:
                    stop_reason = "first_launcher_timeout"
                    break
                if sampling_deadline is not None and now >= sampling_deadline:
                    stop_reason = "window_limit"
                    break
                if stop.is_set():
                    stop_reason = (
                        "context_teardown"
                        if first_launcher_at is not None
                        else "context_teardown_before_first_launcher"
                    )
                    break
                sample_started = now
                try:
                    launchers, rejected = _namespace_launchers(kubectl, namespace)
                    rejected_pods += rejected
                    if first_launcher_at is None:
                        pre_launch_polls += 1
                        if launchers:
                            first_launcher_at = time.monotonic()
                            first_launcher_at_utc = datetime.now(UTC).isoformat()
                            sampling_deadline = first_launcher_at + FULL_SUITE_WINDOW_SECONDS
                    if first_launcher_at is None:
                        stop.wait(max(0.0, FULL_SUITE_INTERVAL_SECONDS - (time.monotonic() - sample_started)))
                        continue
                    record: dict[str, object] = {
                        "sample": sample_count,
                        "observed_at_utc": datetime.now(UTC).isoformat(),
                        "namespace": namespace,
                        "first_launcher_observed_at_utc": first_launcher_at_utc,
                    }
                    if len(launchers) > FULL_SUITE_MAX_LAUNCHERS:
                        launcher_list_truncated = True
                        launchers = launchers[:FULL_SUITE_MAX_LAUNCHERS]
                        list_truncated = True
                    else:
                        list_truncated = False
                    qemus, scan_status = _sample_qemus(proc_root, [pod["pod_uid"] for pod in launchers])
                    sampled_launchers: list[dict[str, object]] = []
                    for pod in launchers:
                        pod_uid = pod["pod_uid"]
                        qemu = qemus[pod_uid]
                        sampled_launchers.append({**pod, "host_qemu": qemu})
                        stats = pod_stats.setdefault(
                            pod_uid,
                            {
                                "pod_uid": pod_uid,
                                "pod_name": pod["pod_name"],
                                "vmi_name": pod["vmi_name"],
                                "vmi_uid": pod["vmi_uid"],
                                "samples": 0,
                                "qemu_process_samples": 0,
                                "process_scan_incomplete_samples": 0,
                                "deletion_timestamp_seen": False,
                            },
                        )
                        _increment_integer_field(stats, "samples")
                        _increment_integer_field(
                            stats, "qemu_process_samples", int(qemu.get("matching_qemu_processes", 0) == 1)
                        )
                        _increment_integer_field(
                            stats, "process_scan_incomplete_samples", int(bool(qemu.get("process_scan_incomplete")))
                        )
                        stats["deletion_timestamp_seen"] = bool(stats["deletion_timestamp_seen"]) or bool(
                            pod["deletion_timestamp"]
                        )
                    record.update(
                        {
                            "launchers": sampled_launchers,
                            "rejected_pod_count": rejected,
                            "launcher_list_truncated": list_truncated,
                            "host_process_scan": scan_status,
                        }
                    )
                except Exception as error:
                    if first_launcher_at is None:
                        pre_launch_polls += 1
                        pre_launch_query_failures += 1
                        stop.wait(max(0.0, FULL_SUITE_INTERVAL_SECONDS - (time.monotonic() - sample_started)))
                        continue
                    query_failures += 1
                    record = {
                        "sample": sample_count,
                        "observed_at_utc": datetime.now(UTC).isoformat(),
                        "namespace": namespace,
                        "first_launcher_observed_at_utc": first_launcher_at_utc,
                        "capture_error": type(error).__name__,
                    }
                if not _append_bounded(destination, record, max_bytes=FULL_SUITE_MAX_BYTES):
                    stop_reason = "byte_limit"
                    break
                sample_count += 1
                if sample_count >= FULL_SUITE_MAX_SAMPLES:
                    stop_reason = "sample_limit"
                    break
                stop.wait(max(0.0, FULL_SUITE_INTERVAL_SECONDS - (time.monotonic() - sample_started)))
            clean_exit = True
        finally:
            _write_full_suite_summary(
                summary_path,
                {
                    "status": "observer_stopped" if clean_exit else "observer_failed",
                    "collector_exited_cleanly": clean_exit,
                    "namespace": namespace,
                    "started_at_utc": started_at,
                    "first_launcher_observed_at_utc": first_launcher_at_utc,
                    "finished_at_utc": datetime.now(UTC).isoformat(),
                    "window_seconds": FULL_SUITE_WINDOW_SECONDS,
                    "pre_launch_wait_seconds": FULL_SUITE_PRE_LAUNCH_SECONDS,
                    "pre_launch_poll_count": pre_launch_polls,
                    "pre_launch_query_failures": pre_launch_query_failures,
                    "sampling_elapsed_seconds": round(
                        max(0.0, time.monotonic() - first_launcher_at) if first_launcher_at is not None else 0.0, 3
                    ),
                    "sample_limit": FULL_SUITE_MAX_SAMPLES,
                    "samples_written": sample_count,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "stop_reason": stop_reason,
                    "window_exhausted": stop_reason in {"window_limit", "sample_limit"},
                    "query_failures": query_failures,
                    "rejected_pod_count": rejected_pods,
                    "launcher_list_truncated": launcher_list_truncated,
                    "raw_bytes": destination.stat().st_size,
                    "raw_bytes_truncated": stop_reason == "byte_limit",
                    "pods": [pod_stats[uid] for uid in sorted(pod_stats)],
                },
            )

    thread = threading.Thread(target=collect, name="vm-full-suite-boot-rcu-host-observer", daemon=True)
    thread.start()
    return stop, thread


@contextmanager
def capture_namespace(
    kubectl: Callable[..., str], namespace: str, destination: Path, *, proc_root: Path = Path("/proc")
) -> Iterator[None]:
    stop, thread = start_namespace(kubectl, namespace, destination, proc_root=proc_root)
    try:
        yield
    finally:
        stop_capture(stop, thread)


def summarize_guest_serials(artifact_root: Path, namespace: str, host_summary: Path) -> Path:
    """Correlate retained guest observer markers by launcher Pod UID, with explicit gaps."""
    serial_root = artifact_root / "serial"
    destination = artifact_root / "boot-rcu-guest-summary.json"
    host_uids: set[str] = set()
    host_summary_error: str | None = None
    try:
        host_data = json.loads(host_summary.read_text())
        host_uids = {
            str(pod["pod_uid"])
            for pod in host_data.get("pods", [])
            if isinstance(pod, dict) and isinstance(pod.get("pod_uid"), str)
        }
    except OSError, json.JSONDecodeError, TypeError:
        host_summary_error = "unavailable-or-invalid"

    guest_dirs: dict[str, Path] = {}
    prefix = f"{namespace}_virt-launcher-"
    for directory in sorted(serial_root.glob(f"{prefix}*")):
        if not directory.is_dir():
            continue
        try:
            pod_uid = host_qemu._launcher_pod_uid(directory, namespace)
        except ValueError:
            continue
        guest_dirs[pod_uid] = directory

    rows = []
    for pod_uid in sorted(host_uids | set(guest_dirs)):
        guest_directory = guest_dirs.get(pod_uid)
        started: set[int] = set()
        ended: set[int] = set()
        completed_samples: int | None = None
        sample_errors = 0
        bytes_scanned = files_scanned = 0
        truncated = False
        if guest_directory is not None:
            for serial in sorted(guest_directory.rglob("*.log")):
                if files_scanned >= FULL_SUITE_MAX_SERIAL_FILES or bytes_scanned >= FULL_SUITE_MAX_SERIAL_BYTES:
                    truncated = True
                    break
                files_scanned += 1
                remaining = FULL_SUITE_MAX_SERIAL_BYTES - bytes_scanned
                with serial.open("rb") as source:
                    content = source.read(remaining + 1)
                bytes_scanned += min(len(content), remaining)
                if len(content) > remaining:
                    truncated = True
                    content = content[:remaining]
                started.update(
                    int(match) for match in re.findall(rb"AGENTPLANE_BOOT_RCU_SAMPLE_BEGIN seq=(\d+)", content)
                )
                ended.update(int(match) for match in re.findall(rb"AGENTPLANE_BOOT_RCU_SAMPLE_END seq=(\d+)", content))
                sample_errors += len(re.findall(rb"AGENTPLANE_BOOT_RCU_SAMPLE_ERROR", content))
                receipts = re.findall(rb"AGENTPLANE_BOOT_RCU_OBSERVER_COMPLETE samples=(\d+)", content)
                if receipts:
                    completed_samples = int(receipts[-1])
                if truncated:
                    break
        complete = bool(
            completed_samples is not None
            and set(range(completed_samples)) <= (started & ended)
            and sample_errors == 0
            and not truncated
        )
        rows.append(
            {
                "pod_uid": pod_uid,
                "host_observed": pod_uid in host_uids,
                "serial_directory_present": guest_directory is not None,
                "guest_samples_started": len(started),
                "guest_samples_ended": len(ended),
                "guest_completion_receipt_samples": completed_samples,
                "guest_sample_error_count": sample_errors,
                "guest_observer_complete": complete,
                "serial_files_scanned": files_scanned,
                "serial_bytes_scanned": bytes_scanned,
                "serial_scan_truncated": truncated,
                "status": "complete"
                if complete
                else "missing"
                if guest_directory is None
                else "partial-or-not-finished",
            }
        )
    summary: dict[str, object] = {
        "namespace": namespace,
        "host_summary": str(host_summary.name),
        "host_summary_error": host_summary_error,
        "host_observed_pod_count": len(host_uids),
        "guest_serial_pod_count": len(guest_dirs),
        "rows": rows,
        "interpretation": "Guest samples use guest time; host samples use runner monotonic time. Partial guest samples do not establish host-side absence.",
    }
    _write_full_suite_summary(destination, summary)
    return destination


def _write_summary(
    destination: Path, *, stop_reason: str, samples_written: int, clean_exit: bool, capture_errors: int
) -> None:
    summary = destination.with_name(destination.stem + "-summary.json")
    encoded = (
        json.dumps(
            {
                "stop_reason": stop_reason,
                "samples_written": samples_written,
                "clean_exit": clean_exit,
                "capture_errors": capture_errors,
                "observed_at_utc": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise ValueError("boot RCU observer summary exceeded its byte bound")
    temporary = summary.with_name(summary.name + ".partial")
    temporary.write_bytes(encoded)
    temporary.replace(summary)


def start(
    kubectl: Callable[..., str], namespace: str, vmi_uid: str, destination: Path, *, proc_root: Path = Path("/proc")
) -> tuple[threading.Event, threading.Thread]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"")
    _write_summary(destination, stop_reason="not_finished", samples_written=0, clean_exit=False, capture_errors=0)
    stop = threading.Event()

    def collect() -> None:
        deadline = time.monotonic() + MAX_SECONDS
        pod: tuple[str, str] | None = None
        samples_written = 0
        capture_errors = 0
        stop_reason = "collector_error"
        clean_exit = False
        try:
            for sample in range(MAX_SAMPLES):
                if stop.is_set():
                    stop_reason = "teardown"
                    break
                if time.monotonic() >= deadline:
                    stop_reason = "deadline"
                    break
                started = time.monotonic()
                try:
                    if pod is None:
                        pod = _launcher_pod(kubectl, namespace, vmi_uid)
                    record: dict[str, object] = {
                        "sample": sample,
                        "observed_at_utc": datetime.now(UTC).isoformat(),
                        "namespace": namespace,
                        "vmi_uid": vmi_uid,
                        "pod_name": pod[0] if pod else None,
                        "pod_uid": pod[1] if pod else None,
                        "host_qemu": _sample_qemu(proc_root, pod[1]) if pod else {"pod_pending": True},
                        "qmp": "disabled-passive-baseline",
                    }
                except Exception as error:
                    capture_errors += 1
                    record = {
                        "sample": sample,
                        "capture_error": type(error).__name__,
                        "observed_at_utc": datetime.now(UTC).isoformat(),
                    }
                if not _append_bounded(destination, record):
                    stop_reason = "max_bytes"
                    break
                samples_written += 1
                stop.wait(max(0.0, INTERVAL_SECONDS - (time.monotonic() - started)))
            else:
                stop_reason = "max_samples"
            clean_exit = True
        finally:
            _write_summary(
                destination,
                stop_reason=stop_reason,
                samples_written=samples_written,
                clean_exit=clean_exit,
                capture_errors=capture_errors,
            )

    thread = threading.Thread(target=collect, name="vm-boot-rcu-host-observer", daemon=True)
    thread.start()
    return stop, thread


def stop_capture(stop: threading.Event, thread: threading.Thread) -> None:
    stop.set()
    thread.join(timeout=3)
    if thread.is_alive():
        raise TimeoutError("boot RCU host observer did not stop within its bounded API timeout")


@contextmanager
def capture(kubectl: Callable[..., str], namespace: str, vmi_uid: str, destination: Path) -> Iterator[None]:
    stop, thread = start(kubectl, namespace, vmi_uid, destination)
    try:
        yield
    finally:
        stop_capture(stop, thread)
