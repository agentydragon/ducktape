"""Bounded boot interrupt evidence; monitor sampling also works without trace events.

QMP sampling and interrupt logging perturb TCG timing. These are diagnostics,
never readiness predicates. Samples cannot reconstruct every intervening EOI.
The summary states exactly which trace events the running emulator accepted.
"""

import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    enabled: list[str] = []
    redirected = False
    interrupt_logging = False
    trace: Path | None = None
    trace_stream = None

    def qmp(command: dict[str, Any]) -> Any:
        return _qmp(kubectl, namespace, pod, uid, domain, command)

    def hmp(command: str, cpu: int | None = None) -> Any:
        arguments: dict[str, Any] = {"command-line": command}
        if cpu is not None:
            arguments["cpu-index"] = cpu
        return qmp({"execute": "human-monitor-command", "arguments": arguments})

    def stop_logging() -> None:
        nonlocal interrupt_logging
        for event in enabled[:]:
            try:
                qmp({"execute": "trace-event-set-state", "arguments": {"name": event, "enable": False}})
                enabled.remove(event)
            except Exception as error:
                record["errors"].append(f"disable {event}: {error}")
        if interrupt_logging:
            try:
                result = hmp("log none")
                if result != "":
                    raise RuntimeError(f"log none rejected: {result!r}")
                interrupt_logging = False
            except Exception as error:
                record["errors"].append(f"disable interrupt log: {error}")

    try:
        deadline = time.monotonic() + 30
        while not stop.is_set():
            sampled = boot_rcu._sample_qemu(proc_root, uid)
            if sampled.get("matching_qemu_processes") == 1:
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
        process = proc_root / str(sampled["pid"])
        cmdline = (process / "cmdline").read_bytes().split(b"\0")
        # Never replace an existing emulator logger. Monitor sampling still runs.
        try:
            states = qmp({"execute": "trace-event-get-state", "arguments": {"name": "*"}})
            record["available_apic_events"] = [s for s in states if "apic" in s["name"]]
            candidates = [s["name"] for s in states if s["name"] in EVENTS and s["state"] == "disabled"]
        except Exception as error:
            record["errors"].append(f"trace discovery: {error}")
            candidates = []
        if b"-D" in cmdline or b"-d" in cmdline:
            record["logging_skipped"] = "existing QEMU logging configuration"
        else:
            guest_path = f"/tmp/agentplane-boot-interrupts-{uid}.log"
            trace = process / "root" / guest_path.removeprefix("/")
            try:
                redirected = True  # Restore even when the command's reply is lost.
                if (result := hmp(f"logfile {guest_path}")) != "":
                    raise RuntimeError(f"logfile rejected: {result!r}")
                for event in candidates:
                    enabled.append(event)  # Also disable an uncertain activation.
                    try:
                        qmp({"execute": "trace-event-set-state", "arguments": {"name": event, "enable": True}})
                        record["accepted_events"].append(event)
                    except Exception as error:
                        record["errors"].append(f"enable {event}: {error}")
                interrupt_logging = True
                if (result := hmp("log int")) != "":
                    raise RuntimeError(f"log int rejected: {result!r}")
                record["log_int_accepted"] = True
                record["logging_started_at"] = _now()
                # Hold the descriptor across process/Pod deletion, like guest_logs.
                trace_stream = trace.open("rb")
            except Exception as error:
                record["errors"].append(f"logging setup: {error}")
                stop_logging()
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
                if trace_stream is not None:
                    # Keep bounded evidence continuously, before a later process deletion.
                    trace_stream.seek(0)
                    data = trace_stream.read(MAX_BYTES)
                    (output / "interrupts.log").write_bytes(data)
                    record["bytes_retained"] = len(data)
                    if len(data) == MAX_BYTES and (enabled or interrupt_logging):
                        record["logging_stopped_at_byte_limit"] = _now()
                        stop_logging()
                if _userspace(launcher):
                    record["stop_reason"] = "userspace"
                    break
                stop.wait(SAMPLE_SECONDS)
            else:
                record["stop_reason"] = "sample-limit"
    except Exception as error:
        record["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        stop_logging()
        if redirected:
            try:
                if (result := hmp("logfile /proc/self/fd/2")) != "":
                    raise RuntimeError(f"restore logfile rejected: {result!r}")
            except Exception as error:
                record["errors"].append(f"restore stderr: {error}")
        if trace_stream is not None:
            trace_stream.seek(0)
            data = trace_stream.read(MAX_BYTES)
            (output / "interrupts.log").write_bytes(data)
            record["bytes_retained"] = len(data)
            trace_stream.close()
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
