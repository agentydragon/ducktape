"""Opt-in, passive host-side boot samples for the manual two-vCPU TCG VMI."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

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


def _append_bounded(path: Path, record: dict[str, object]) -> bool:
    encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
    if path.stat().st_size + len(encoded) > MAX_BYTES:
        return False
    with path.open("ab") as output:
        output.write(encoded)
    return True


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
