"""Bounded, process-filtered IOTLB provenance before QEMU's section assertion."""

import json
import os
import re
import select
import threading
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

from python.runfiles import Runfiles

from agentplane.system_tests.vm import tracefs
from agentplane.system_tests.vm.iotlb_profile import Profile

ROLLING_BYTES = 1024 * 1024
FAILURE_BYTES = 256 * 1024
METADATA_BYTES = 64 * 1024
LAUNCHER_BYTES = 1536 * 1024
TOTAL_BYTES = 6 * 1024 * 1024
# Launcher files total 1.3125 MiB; four plus the observer metadata fit 6 MiB.
# The 1.5 MiB ceiling is per launcher, including all three files.
MAX_LAUNCHERS = 4
KERNEL_BUFFER_KIB = 2048
MAX_THREADS = 64
MAX_LINE = 16 * 1024
WINDOW_SECONDS = 300.0
DISCOVERY_SECONDS = 600.0
TRACE_ROOT = Path("/sys/kernel/tracing")


@dataclass(frozen=True)
class Target:
    launcher: str
    uid: str
    domain: str
    process: Path
    start_time: str


def process_start(process: Path) -> str:
    stat = (process / "stat").read_text()
    return stat[stat.rfind(")") + 2 :].split()[19]


def find_process(launcher: Path, namespace: str, proc: Path) -> Target | None:
    pod, uid = launcher.name.removeprefix(namespace + "_").rsplit("_", 1)
    if not pod.startswith("virt-launcher-"):
        raise ValueError("Expected launcher Pod directory")
    vmi, separator, _ = pod.removeprefix("virt-launcher-").rpartition("-")
    if not separator:
        raise ValueError("Launcher Pod has no generated suffix")
    domain = f"{namespace}_{vmi}"
    matches = []
    deadline = time.monotonic() + 0.5
    for index, comm in enumerate(proc.glob("[0-9]*/comm")):
        if index >= 4096 or time.monotonic() >= deadline:
            raise RuntimeError("QEMU discovery scan budget exhausted")
        process = comm.parent
        try:
            started = process_start(process)
            if not comm.read_text().startswith("qemu"):
                continue
            args = (process / "cmdline").read_bytes().split(b"\0")
            names = [args[index + 1].split(b",", 1)[0] for index, arg in enumerate(args[:-1]) if arg == b"-name"]
            if names != [f"guest={domain}".encode()]:
                continue
            cgroup = (process / "cgroup").read_text()
            if not any(
                re.search(rf"(?<![A-Za-z0-9_])pod{re.escape(value)}(?=$|[/.])", cgroup)
                for value in (uid, uid.replace("-", "_"))
            ):
                continue
            if started == process_start(process):
                matches.append(Target(launcher.name, uid, domain, process, started))
        except FileNotFoundError:
            continue
    if len(matches) > 1:
        raise RuntimeError("Multiple QEMUs for exact launcher domain and Pod UID")
    return matches[0] if matches else None


def write_trace(path: Path, value: str) -> None:
    # O_TRUNC on uprobe_events removes every dynamic probe; tracefs rejects O_APPEND.
    with os.fdopen(os.open(path, os.O_WRONLY), "w") as stream:
        stream.write(value + "\n")


def pid_filter(tids: set[int]) -> str:
    if not tids or len(tids) > MAX_THREADS:
        raise ValueError("QEMU thread filter outside capture budget")
    return " || ".join(f"common_pid == {tid}" for tid in sorted(tids))


def write_json(path: Path, value: object, limit: int) -> None:
    encoded = (json.dumps(value, separators=(",", ":"), default=str) + "\n").encode()
    if len(encoded) > limit:
        encoded = (json.dumps({"coverage_gap": "metadata-budget", "original_bytes": len(encoded)}) + "\n").encode()
    path.write_bytes(encoded)


@dataclass
class History:
    lines: deque[bytes] = field(default_factory=deque)
    retained_bytes: int = 0
    sequence: int = 0
    overwritten_records: int = 0
    overwritten_bytes: int = 0
    malformed: int = 0
    foreign: int = 0
    fetch_faults_or_invalid_values: int = 0
    events: Counter[str] = field(default_factory=Counter)
    failure: bytes = b""

    def append(self, line: bytes, tids: set[int]) -> None:
        match = re.search(rb"-(\d+)\s+\[\d+\].*?\s(\w+):\s", line)
        if match is None or len(line) > MAX_LINE:
            self.malformed += 1
            return
        if int(match[1]) not in tids:
            self.foreign += 1
            return
        name = match[2].decode()
        if name not in {
            "fill_io",
            "iommu_target",
            "check",
            "failure",
            "access_0",
            "access_1",
            "access_2",
            "access_3",
            "publish",
            "commit_queued",
            "commit_direct",
            "commit_cpu",
            "flush_begin",
            "flush_done",
        }:
            self.malformed += 1
            return
        self.events[name] += 1
        # Scalar tracefs fetches can return zero on EFAULT. Required pointer/count
        # zeros are therefore evidence gaps, not usable zero-valued map observations.
        if b"(fault)" in line or re.search(rb"\b(?:cpu|dispatch|sections|sections_nb)=0(?:x0)?(?:\s|$)", line):
            self.fetch_faults_or_invalid_values += 1
        self.sequence += 1
        encoded = str(self.sequence).encode() + b" " + line
        if name == "failure":
            self.failure = encoded
        self.lines.append(encoded)
        self.retained_bytes += len(encoded)
        while self.retained_bytes > ROLLING_BYTES:
            removed = self.lines.popleft()
            self.retained_bytes -= len(removed)
            self.overwritten_records += 1
            self.overwritten_bytes += len(removed)

    def save(self, output: Path) -> None:
        (output / "rolling.log").write_bytes(b"".join(self.lines))
        if self.failure:
            # Raw bytes avoid JSON escaping expansion. The failure marker has its
            # own reserve even when it was followed by a full burst on another CPU.
            tail: deque[bytes] = deque()
            size = len(self.failure)
            for line in reversed(self.lines):
                if size + len(line) > FAILURE_BYTES:
                    break
                tail.appendleft(line)
                size += len(line)
            (output / "failure.log").write_bytes(self.failure + b"".join(tail))

    def counters(self) -> dict[str, object]:
        return {
            "events": dict(self.events),
            "records": self.sequence,
            "retained_bytes": self.retained_bytes,
            "overwritten_records": self.overwritten_records,
            "overwritten_bytes": self.overwritten_bytes,
            "malformed_records": self.malformed,
            "foreign_records": self.foreign,
            "fetch_faults_or_invalid_values": self.fetch_faults_or_invalid_values,
            "scalar_fetch_faults_indistinguishable_from_zero": True,
            "failure_seen": bool(self.failure),
        }


@dataclass
class Record:
    target: Target
    started: float = field(default_factory=time.monotonic)
    attached: float | None = None
    finished: float | None = None
    stop_reason: str = "setup-failed"
    gaps: list[str] = field(
        default_factory=lambda: ["history-before-attachment-unavailable", "thread-discovery-interval"]
    )
    errors: list[str] = field(default_factory=list)
    threads: list[int] = field(default_factory=list)
    new_threads: int = 0
    trace_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    kernel_buffer_kib: int = 0
    failure_observed: bool = False
    profile: str = ""

    def error(self, error: Exception) -> None:
        if len(self.errors) < 16:
            self.errors.append(f"{type(error).__name__}: {error}"[:512])


def drain(pipe: int, history: History, tids: set[int], pending: bytes) -> tuple[bytes, int]:
    read_bytes = 0
    # Give identity/thread checks a turn even when the producer continuously fills the ring.
    for _ in range(4):
        try:
            data = os.read(pipe, 65536)
        except BlockingIOError:
            break
        if not data:
            break
        read_bytes += len(data)
        lines = (pending + data).splitlines(keepends=True)
        pending = b""
        for line in lines:
            if line.endswith(b"\n"):
                history.append(line, tids)
            else:
                pending = line
        if len(pending) > MAX_LINE:
            raise RuntimeError("IOTLB trace line exceeds parsing budget")
    return pending, read_bytes


def trace_stats(instance: Path) -> dict[str, dict[str, int]]:
    result = {}
    for path in (instance / "per_cpu").glob("cpu*/stats"):
        values = {}
        for line in path.read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator and key in {"overrun", "commit overrun", "dropped events", "entries", "read events"}:
                values[key] = int(value.strip())
        result[path.parent.name] = values
    return result


@dataclass
class ActiveTrace:
    instance: Path
    event_root: Path
    failure_instance: Path
    failure_event: Path

    def update_threads(self, tids: set[int], profile: Profile) -> None:
        expression = pid_filter(tids)
        write_trace(self.failure_event / "filter", expression)
        for probe in profile.probes:
            if probe.name != "failure":
                write_trace(self.event_root / probe.name / "filter", expression)


@contextmanager
def probes(target: Target, profile: Profile, record: Record, *, trace_root: Path = TRACE_ROOT) -> Iterator[ActiveTrace]:
    with ExitStack() as resources:

        def cleanup(callback: Callable[[], object]) -> None:
            # ExitStack must finish removing our other resources after one cleanup fault.
            try:
                callback()
            except Exception as error:
                record.error(error)
                record.gaps.append("cleanup-error")

        binary = (target.process / "exe").read_bytes()
        profile.verify(binary)
        record.profile = profile.binary_sha256
        tracefs.ensure_mounted(trace_root)
        group = "ap_iotlb_" + uuid.uuid4().hex
        instance = trace_root / "instances" / group
        instance.mkdir()
        resources.callback(cleanup, instance.rmdir)
        write_trace(instance / "tracing_on", "0")
        failure_instance = trace_root / "instances" / (group + "_failure")
        failure_instance.mkdir()
        resources.callback(cleanup, failure_instance.rmdir)
        write_trace(failure_instance / "tracing_on", "0")
        cpus = len(list((instance / "per_cpu").glob("cpu*")))
        write_trace(instance / "buffer_size_kb", str(min(128, max(1, KERNEL_BUFFER_KIB // max(1, cpus) - 4))))
        write_trace(failure_instance / "buffer_size_kb", "4")
        record.kernel_buffer_kib = sum(
            int((path / "buffer_total_size_kb").read_text().split()[0]) for path in (instance, failure_instance)
        )
        if record.kernel_buffer_kib > KERNEL_BUFFER_KIB:
            raise RuntimeError("Tracefs rounded kernel buffers beyond observer budget")
        write_trace(instance / "trace_clock", "mono")
        write_trace(failure_instance / "trace_clock", "mono")
        for probe in profile.probes:
            definition = f"p:{group}/{probe.name} {target.process}/exe:{probe.offset:#x} "
            definition += " ".join(f"{key}={value}" for key, value in probe.arguments.items())
            write_trace(trace_root / "uprobe_events", definition)
            resources.callback(cleanup, partial(write_trace, trace_root / "uprobe_events", f"-:{group}/{probe.name}"))
        event_root = instance / "events" / group
        resources.callback(cleanup, lambda: write_trace(event_root / "enable", "0"))
        tids = {int(path.name) for path in (target.process / "task").iterdir()}
        record.threads = sorted(tids)
        failure_event = failure_instance / "events" / group / "failure"
        resources.callback(cleanup, lambda: write_trace(failure_event / "enable", "0"))
        active = ActiveTrace(instance, event_root, failure_instance, failure_event)
        active.update_threads(tids, profile)
        # 5.15's uprobe traceoff acts on the top-level trace array; snapshot
        # triggers reject private instances. Reserve a separate failure-only ring.
        for probe in profile.probes:
            if probe.name != "failure":
                write_trace(event_root / probe.name / "enable", "1")
        write_trace(failure_event / "enable", "1")
        resources.callback(cleanup, lambda: write_trace(instance / "tracing_on", "0"))
        resources.callback(cleanup, lambda: write_trace(failure_instance / "tracing_on", "0"))
        if process_start(target.process) != target.start_time:
            raise RuntimeError("QEMU identity changed during probe attachment")
        record.attached = time.monotonic()
        write_trace(failure_instance / "tracing_on", "1")
        write_trace(instance / "tracing_on", "1")
        try:
            yield active
        finally:
            write_trace(instance / "tracing_on", "0")
            write_trace(failure_instance / "tracing_on", "0")
            record.trace_stats = {
                **{"history/" + key: value for key, value in trace_stats(instance).items()},
                **{"failure/" + key: value for key, value in trace_stats(failure_instance).items()},
            }


def capture_one(
    target: Target, profile: Profile, output: Path, stop: threading.Event, *, trace_root: Path = TRACE_ROOT
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    record = Record(target=target)
    history = History()
    pending = failure_pending = b""
    try:
        with probes(target, profile, record, trace_root=trace_root) as active:
            instance = active.instance
            deadline = time.monotonic() + WINDOW_SECONDS
            tids = set(record.threads)
            with ExitStack() as pipes:
                pipe = pipes.enter_context(
                    os.fdopen(os.open(instance / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0)
                )
                failure_pipe = pipes.enter_context(
                    os.fdopen(
                        os.open(active.failure_instance / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0
                    )
                )
                record.stop_reason = "window-limit"
                while time.monotonic() < deadline:
                    failure_pending, _ = drain(failure_pipe.fileno(), history, tids, failure_pending)
                    if history.failure:
                        record.failure_observed = True
                        record.stop_reason = "assertion-observed"
                        record.gaps.append("failure-to-userspace-stop-window")
                        break
                    pending, _ = drain(pipe.fileno(), history, tids, pending)
                    if stop.is_set():
                        record.stop_reason = "teardown"
                        break
                    try:
                        if process_start(target.process) != target.start_time:
                            record.gaps.append("process-identity-changed")
                            record.stop_reason = "process-identity-changed"
                            break
                        current = {int(path.name) for path in (target.process / "task").iterdir()}
                    except FileNotFoundError:
                        record.stop_reason = "process-exited"
                        break
                    if len(tids | current) > MAX_THREADS:
                        record.stop_reason = "thread-limit"
                        record.gaps.append("thread-limit")
                        break
                    if current != set(record.threads):
                        record.new_threads += len(current - tids)
                        record.gaps.append("thread-start-or-exit-before-filter-update")
                        active.update_threads(current, profile)
                        tids |= current
                        record.threads = sorted(current)
                    select.select([pipe, failure_pipe], [], [], 0.1)
                write_trace(instance / "tracing_on", "0")
                write_trace(active.failure_instance / "tracing_on", "0")
                failure_pending, _ = drain(failure_pipe.fileno(), history, tids, failure_pending)
                record.failure_observed = bool(history.failure)
                # Producers are frozen; fully consume the finite private ring, not one chunk.
                for _ in range(KERNEL_BUFFER_KIB // 64 + 2):
                    pending, read_bytes = drain(pipe.fileno(), history, tids, pending)
                    if not read_bytes:
                        break
            if pending or failure_pending:
                record.gaps.append("partial-final-record")
    except Exception as error:
        record.error(error)
        record.gaps.append("collector-error")
    finally:
        if history.failure:
            record.gaps.append("failure-to-userspace-stop-window")
        if history.overwritten_records:
            record.gaps.append("rolling-history-overwritten")
        if history.malformed or history.foreign or history.fetch_faults_or_invalid_values:
            record.gaps.append("trace-records-unusable")
        if any(
            values.get(key, 0)
            for values in record.trace_stats.values()
            for key in ("overrun", "commit overrun", "dropped events")
        ):
            record.gaps.append("kernel-events-lost")
        record.gaps = sorted(set(record.gaps))
        record.finished = time.monotonic()
        try:
            history.save(output)
        except OSError as error:
            record.error(error)
            record.gaps.append("artifact-write-error")
        write_json(output / "metadata.json", {**asdict(record), "history": history.counters()}, METADATA_BYTES)


@contextmanager
def capture(namespace: str, artifact_root: Path, *, proc: Path = Path("/proc")) -> Iterator[None]:
    """Attach at exact-domain discovery while host_qemu owns the observer lifetime."""
    stop = threading.Event()

    def observe() -> None:
        seen: set[str] = set()
        discovered: set[str] = set()
        errors: list[str] = []
        capped = False
        try:
            manifest = (
                Runfiles.CreateOrRaise().root() / "ducktape/agentplane/system_tests/vm/verified-iotlb-layout.json"
            )
            profile = Profile.model_validate_json(manifest.read_bytes())
            deadline = time.monotonic() + DISCOVERY_SECONDS
            with ThreadPoolExecutor(max_workers=MAX_LAUNCHERS, thread_name_prefix="vm-iotlb") as workers:
                tasks = []
                while not stop.is_set() and time.monotonic() < deadline:
                    for launcher in sorted((artifact_root / "serial").glob(f"{namespace}_virt-launcher-*")):
                        if len(discovered) < MAX_LAUNCHERS + 1:
                            discovered.add(launcher.name)
                        if launcher.name in seen:
                            continue
                        if len(seen) >= MAX_LAUNCHERS:
                            capped = True
                            continue
                        try:
                            target = find_process(launcher, namespace, proc)
                            if target is not None:
                                seen.add(launcher.name)
                                tasks.append(
                                    workers.submit(
                                        capture_one,
                                        target,
                                        profile,
                                        artifact_root / "host-qemu" / launcher.name / "iotlb",
                                        stop,
                                    )
                                )
                        except Exception as error:
                            if len(errors) < 16:
                                errors.append(f"{type(error).__name__}: {error}"[:512])
                    stop.wait(0.25)
                for task in tasks:
                    task.result()
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}"[:512])
        finally:
            output = artifact_root / "host-qemu"
            output.mkdir(parents=True, exist_ok=True)
            write_json(
                output / "iotlb-observer.json",
                {
                    "launchers": sorted(seen),
                    "unattached": sorted(discovered - seen)[:MAX_LAUNCHERS],
                    "launcher_limit_reached": capped,
                    "discovery_expired": not stop.is_set(),
                    "errors": errors,
                },
                METADATA_BYTES,
            )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="vm-iotlb-discovery") as executor:
        observing = executor.submit(observe)
        try:
            yield
        finally:
            stop.set()
            # A failed artifact filesystem cannot be reported to that same filesystem.
            with suppress(OSError):
                observing.result()
