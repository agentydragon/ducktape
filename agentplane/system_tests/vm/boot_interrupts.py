"""Bounded QEMU APIC USDT transitions and monitor snapshots during guest boot.

The launcher's DTrace backend cannot enable APIC events through QMP. Kernel
uprobes collect its existing SDT sites, with thread identity and loss counters.
Both probes and monitor queries perturb timing; neither predicates readiness.
"""

import json
import os
import re
import select
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agentplane.system_tests.vm import boot_rcu, host_qemu

EVENTS = (
    "apic_local_deliver",
    "apic_deliver_irq",
    "apic_register_write",
    "ioapic_set_remote_irr",
    "ioapic_clear_remote_irr",
    "ioapic_eoi_broadcast",
    "ioapic_eoi_delayed_reassert",
    "ioapic_set_irq",
)
TRACE_ROOT = Path("/sys/kernel/tracing")
TRACE_BUFFER_KIB = 128
_trace_mount_lock = threading.Lock()
DISCOVERY_SECONDS = 600.0
WINDOW_SECONDS = 300.0
SAMPLE_SECONDS = 2.0
MAX_LAUNCHERS = 4
MAX_BYTES = 16 * 1024 * 1024
MAX_SAMPLES = 150


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _qmp(kubectl: host_qemu.Kubectl, namespace: str, pod: str, uid: str, domain: str, command: dict[str, Any]) -> Any:
    metadata = json.loads(kubectl("get", "pod", pod, "-n", namespace, "-o", "json", timeout=2))["metadata"]
    if metadata["uid"] != uid:
        raise RuntimeError("Boot interrupt launcher Pod UID changed")
    result = json.loads(
        kubectl(
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            "compute",
            "--",
            "virsh",
            "-c",
            host_qemu.LIBVIRT_URI,
            "qemu-monitor-command",
            domain,
            json.dumps(command),
            timeout=2,
            stdout_only=True,
        )
    )
    if "error" in result or "return" not in result:
        raise RuntimeError(f"Boot interrupt QMP failed: {result!r}")
    return result["return"]


def _userspace(launcher: Path) -> bool:
    for serial in launcher.glob("*/*.log"):
        try:
            with serial.open("rb") as stream:
                if b"systemd[1]:" in stream.read(1024 * 1024):
                    return True
        except FileNotFoundError:
            continue
    return False


@dataclass(frozen=True)
class _Probe:
    name: str
    offset: int
    semaphore: int
    arguments: str
    kind: Literal["p", "r"] = "p"


def _apic_probes(executable: Path) -> list[_Probe]:
    """Decode x86-64 SDT sites and the exported interrupt acceptance function."""
    with executable.open("rb") as stream:
        header = stream.read(64)
        if header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 62:
            raise ValueError("APIC SDT capture requires a little-endian x86-64 ELF executable")
        program_offset, section_offset = struct.unpack_from("<QQ", header, 32)
        program_size, program_count, section_size, section_count = struct.unpack_from("<HHHH", header, 54)
        segments = []
        for index in range(program_count):
            stream.seek(program_offset + index * program_size)
            segment = struct.unpack("<IIQQQQQQ", stream.read(56))
            if segment[0] == 1:  # PT_LOAD
                segments.append((segment[2], segment[3], segment[5]))

        def file_offset(address: int) -> int:
            for offset, virtual, size in segments:
                if virtual <= address < virtual + size:
                    return offset + address - virtual
            raise ValueError(f"SDT address {address:#x} is outside ELF load segments")

        sections = []
        for index in range(section_count):
            stream.seek(section_offset + index * section_size)
            sections.append(struct.unpack("<IIQQQQIIQQ", stream.read(64)))
        probes = []
        for section in sections:
            if section[1] == 11:  # SHT_DYNSYM: exported TCG interrupt acceptance.
                strings = sections[section[6]]
                if max(section[5], strings[5]) > 4 * 1024 * 1024:
                    raise ValueError("ELF dynamic symbol section exceeds capture metadata budget")
                stream.seek(strings[4])
                names = stream.read(strings[5])
                stream.seek(section[4])
                symbols = stream.read(section[5])
                for entry in range(0, len(symbols), section[9]):
                    name, info, _other, defined, value, _size = struct.unpack_from("<IBBHQQ", symbols, entry)
                    if defined and info & 15 == 2 and names.startswith(b"apic_get_interrupt\0", name):
                        probes.append(_Probe("apic_get_interrupt", file_offset(value), 0, "", "r"))
                        break
            if section[1] != 7:  # SHT_NOTE
                continue
            if section[5] > 4 * 1024 * 1024:
                raise ValueError("ELF note section exceeds capture metadata budget")
            stream.seek(section[4])
            notes = stream.read(section[5])
            position = 0
            while position < len(notes):
                name_size, desc_size, kind = struct.unpack_from("<III", notes, position)
                position += 12
                name = notes[position : position + name_size]
                position += (name_size + 3) & ~3
                desc = notes[position : position + desc_size]
                position += (desc_size + 3) & ~3
                if name != b"stapsdt\0" or kind != 3:
                    continue
                location, _base, semaphore = struct.unpack_from("<QQQ", desc)
                provider, event, arguments = desc[24:].split(b"\0", 2)
                if provider == b"qemu" and event.decode() in EVENTS:
                    probes.append(
                        _Probe(
                            event.decode(),
                            file_offset(location),
                            file_offset(semaphore) if semaphore else 0,
                            arguments.rstrip(b"\0").decode(),
                        )
                    )
        return probes


def _fetch_arguments(arguments: str) -> str:
    # GCC's x86 SDT operands are register, base+offset, or immediate values.
    # Reject unfamiliar operands instead of silently recording the wrong value.
    fields = []
    for index, operand in enumerate(arguments.split()):
        match = re.fullmatch(r"(-?[1248])@(.+)", operand)
        if match is None:
            raise ValueError(f"Unsupported SDT argument: {operand}")
        size, expression = match.groups()
        register = re.fullmatch(r"%([a-z0-9]+)", expression)
        memory = re.fullmatch(r"(-?\d*)\(%([a-z0-9]+)\)", expression)

        def kernel_register(name: str) -> str:
            if name in {
                "eax",
                "ebx",
                "ecx",
                "edx",
                "esi",
                "edi",
                "esp",
                "ebp",
                "rax",
                "rbx",
                "rcx",
                "rdx",
                "rsi",
                "rdi",
                "rsp",
                "rbp",
            }:
                return name[1:]
            if re.fullmatch(r"r(?:[89]|1[0-5])d?", name):
                return name.removesuffix("d")
            raise ValueError(f"Unsupported SDT register: {name}")

        if register:
            fetch = "%" + kernel_register(register[1])
        elif memory:
            fetch = f"{int(memory[1] or 0):+d}(%{kernel_register(memory[2])})"
        elif re.fullmatch(r"\$-?\d+", expression):
            fetch = "\\" + expression[1:]
        else:
            raise ValueError(f"Unsupported SDT operand: {expression}")
        fields.append(f"arg{index + 1}={fetch}:{'s' if size.startswith('-') else 'u'}{abs(int(size)) * 8}")
    return " ".join(fields)


def _write_trace(path: Path, value: str) -> None:
    # tracefs rejects O_APPEND; O_TRUNC on uprobe_events deletes *all* probes.
    with os.fdopen(os.open(path, os.O_WRONLY), "w") as stream:
        stream.write(value + "\n")


def _drain_trace(instance: Path, destination: Path, stop: threading.Event, record: dict[str, Any]) -> None:
    with (
        os.fdopen(os.open(instance / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0) as pipe,
        destination.open("wb") as output,
    ):
        retained = 0
        while True:
            ready, _, _ = select.select([pipe], [], [], 0.1)
            if ready:
                try:
                    data = os.read(pipe.fileno(), min(65536, MAX_BYTES - retained))
                except BlockingIOError:
                    data = b""
                output.write(data)
                output.flush()
                retained += len(data)
                record["bytes_retained"] = retained
                if retained == MAX_BYTES:
                    _write_trace(instance / "tracing_on", "0")
                    _write_trace(instance / "events" / "enable", "0")
                    record["logging_stopped_at_byte_limit"] = _now()
                    return
                if data:
                    continue
            if stop.is_set():
                return


@contextmanager
def _capture_apic(
    process: Path, output: Path, record: dict[str, Any], *, trace_root: Path = TRACE_ROOT
) -> Iterator[None]:
    """Own only this launcher's dynamic events and private tracefs instance."""
    with ExitStack() as resources:

        def cleanup(action: str, callback: Any, *args: Any) -> None:
            try:
                callback(*args)
            except Exception as error:
                record["errors"].append(f"{action}: {error}")

        try:
            probes = _apic_probes(process / "exe")
            record["sdt_probes"] = [asdict(probe) for probe in probes]
            if not probes:
                raise RuntimeError("No APIC SDT sites in launcher executable")
            with _trace_mount_lock:
                if not (trace_root / "uprobe_events").exists():
                    subprocess.run(
                        ["mount", "-t", "tracefs", "tracefs", str(trace_root)],
                        check=True,
                        capture_output=True,
                        timeout=2,
                    )
            group = f"agentplane_boot_{record['target']['pod_uid'].replace('-', '')}"
            instance = trace_root / "instances" / group
            instance.mkdir()
            resources.callback(cleanup, "remove trace instance", instance.rmdir)
            _write_trace(instance / "tracing_on", "0")
            _write_trace(instance / "buffer_size_kb", str(TRACE_BUFFER_KIB))
            _write_trace(instance / "trace_clock", "mono")
            _write_trace(instance / "set_event_pid", " ".join(task.name for task in (process / "task").iterdir()))
            _write_trace(instance / "options" / "event-fork", "1")
            record["trace_pid"] = int(process.name)
            record["trace_buffer_kib_per_cpu"] = TRACE_BUFFER_KIB
            for index, probe in enumerate(probes):
                name = f"{probe.name}_{index}"
                reference = f"({probe.semaphore:#x})" if probe.semaphore else ""
                arguments = "vector=$retval:s32" if probe.kind == "r" else _fetch_arguments(probe.arguments)
                definition = f"{probe.kind}:{group}/{name} {process}/exe:{probe.offset:#x}{reference} {arguments}"
                _write_trace(trace_root / "uprobe_events", definition)
                resources.callback(
                    cleanup, f"remove {name}", _write_trace, trace_root / "uprobe_events", f"-:{group}/{name}"
                )
            # Add this last so it is disabled before removing any registered events.
            resources.callback(
                cleanup, "disable APIC events", _write_trace, instance / "events" / group / "enable", "0"
            )
            _write_trace(instance / "events" / group / "enable", "1")
            record["accepted_events"] = sorted({probe.name for probe in probes})
            record["missing_transition_probes"] = sorted(
                {"apic_get_interrupt", "apic_register_write"} - set(record["accepted_events"])
            )
            stop = threading.Event()
            executor = resources.enter_context(ThreadPoolExecutor(max_workers=1))
            _write_trace(instance / "tracing_on", "1")
            record["logging_started_at"] = _now()
            draining = executor.submit(_drain_trace, instance, output / "interrupts.log", stop, record)

            def finish() -> None:
                try:
                    _write_trace(instance / "tracing_on", "0")
                finally:
                    stop.set()
                draining.result()
                record["logging_finished_at"] = _now()
                record["trace_stats"] = {
                    stats.parent.name: stats.read_text() for stats in sorted((instance / "per_cpu").glob("cpu*/stats"))
                }
                record["uprobe_profile"] = [
                    line
                    for line in (trace_root / "uprobe_profile").read_text().splitlines()
                    if str(process / "exe") in line
                ]

            resources.callback(cleanup, "finish APIC capture", finish)
        except Exception as error:
            record["errors"].append(f"APIC SDT capture unavailable: {error}")
            resources.close()
        yield


def _trace_one(
    kubectl: host_qemu.Kubectl,
    namespace: str,
    target: dict[str, str],
    root: Path,
    stop: threading.Event,
    *,
    proc_root: Path = Path("/proc"),
) -> None:
    pod, uid = target["pod_name"], target["pod_uid"]
    domain = f"{namespace}_{target['vmi_name']}"
    output = root / "boot-interrupts" / uid
    output.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "target": target,
        "started_at": _now(),
        "accepted_events": [],
        "errors": [],
        "sampling_interval_seconds": SAMPLE_SECONDS,
        "sample_limit": MAX_SAMPLES,
        "window_seconds": WINDOW_SECONDS,
        "interrupt_log_byte_limit": MAX_BYTES,
    }

    def qmp(command: dict[str, Any]) -> Any:
        return _qmp(kubectl, namespace, pod, uid, domain, command)

    def hmp(command: str, cpu: int | None = None) -> Any:
        arguments: dict[str, Any] = {"command-line": command}
        if cpu is not None:
            arguments["cpu-index"] = cpu
        return qmp({"execute": "human-monitor-command", "arguments": arguments})

    with ExitStack() as resources:
        try:
            deadline = time.monotonic() + 30
            traced_pid = None
            while not stop.is_set():
                sampled = boot_rcu._sample_qemu(proc_root, uid)
                if sampled.get("matching_qemu_processes") == 1:
                    if traced_pid is None:
                        traced_pid = sampled["pid"]
                        resources.enter_context(_capture_apic(proc_root / str(traced_pid), output, record))
                    elif traced_pid != sampled["pid"]:
                        record["trace_pid_changed"] = {"from": traced_pid, "to": sampled["pid"]}
                    try:
                        if qmp({"execute": "query-status"}).get("running"):
                            break
                    except Exception as error:
                        record["last_startup_error"] = str(error)
                if time.monotonic() >= deadline:
                    raise RuntimeError("No Running QEMU for the exact launcher within observer startup budget")
                stop.wait(0.25)
            if stop.is_set():
                record["stop_reason"] = "teardown-before-qemu"
                return
            record["host_before"] = sampled
            deadline = time.monotonic() + WINDOW_SECONDS
            launcher = root / "serial" / f"{namespace}_{pod}_{uid}"
            with (output / "timeline.jsonl").open("w") as timeline:
                for index in range(MAX_SAMPLES):
                    if stop.is_set() or time.monotonic() >= deadline:
                        record["stop_reason"] = "teardown" if stop.is_set() else "window-limit"
                        break
                    sample: dict[str, Any] = {"index": index, "started_at": _now(), "probes": []}
                    for command, cpu in (("info lapic", 0), ("info lapic", 1), ("info pic", None)):
                        probe: dict[str, Any] = {"command": command, "cpu": cpu, "observed_at": _now()}
                        try:
                            probe["return"] = hmp(command, cpu)
                        except Exception as error:
                            probe["error"] = str(error)
                        sample["probes"].append(probe)
                    sample["finished_at"] = _now()
                    timeline.write(json.dumps(sample) + "\n")
                    timeline.flush()
                    record["samples"] = index + 1
                    if _userspace(launcher):
                        record["stop_reason"] = "userspace"
                        break
                    stop.wait(SAMPLE_SECONDS)
                else:
                    record["stop_reason"] = "sample-limit"
        except Exception as error:
            record["errors"].append(f"{type(error).__name__}: {error}")
        finally:
            resources.close()
            record["finished_at"] = _now()
            (output / "summary.json").write_text(json.dumps(record, indent=2) + "\n")


@contextmanager
def capture(kubectl: host_qemu.Kubectl, namespace: str, root: Path) -> Iterator[None]:
    stop = threading.Event()

    def observe() -> None:
        seen: set[str] = set()
        errors: list[str] = []
        deadline = time.monotonic() + DISCOVERY_SECONDS
        try:
            with ThreadPoolExecutor(max_workers=MAX_LAUNCHERS) as workers:
                tasks = []
                while not stop.is_set() and time.monotonic() < deadline and len(seen) < MAX_LAUNCHERS:
                    try:
                        targets, _ = boot_rcu._namespace_launchers(kubectl, namespace)
                        for target in targets:
                            if target["pod_uid"] not in seen and len(seen) < MAX_LAUNCHERS:
                                seen.add(target["pod_uid"])
                                tasks.append(workers.submit(_trace_one, kubectl, namespace, target, root, stop))
                    except Exception as error:
                        # Discovery can race namespace/Pod creation. Retain bounded errors.
                        if len(errors) < 10:
                            errors.append(str(error))
                    stop.wait(1)
                for task in tasks:
                    task.result()
        except Exception as error:
            errors.append(str(error))
        finally:
            output = root / "boot-interrupts"
            output.mkdir(parents=True, exist_ok=True)
            (output / "observer.json").write_text(json.dumps({"pod_uids": sorted(seen), "errors": errors}) + "\n")

    with ThreadPoolExecutor(max_workers=1) as executor:
        observing = executor.submit(observe)
        try:
            yield
        finally:
            stop.set()
            observing.result()
