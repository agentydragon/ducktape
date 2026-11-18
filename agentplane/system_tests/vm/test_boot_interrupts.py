"""Boot diagnostics retain SDT transitions and survive unavailable kernel probes."""

import json
import os
import struct
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agentplane.system_tests.vm import boot_interrupts


def test_changed_pod_uid_prevents_monitor_command() -> None:
    calls = []

    def kubectl(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        return json.dumps({"metadata": {"uid": "replacement"}})

    with pytest.raises(RuntimeError, match="UID changed"):
        boot_interrupts._qmp(kubectl, "scope", "launcher", "original", "domain", {"execute": "query-status"})
    assert len(calls) == 1


def test_sdt_notes_map_virtual_addresses_and_keep_duplicate_sites(tmp_path: Path) -> None:
    # ELF64 SDT wire format: stripped executables keep SHT_NOTE. Include a
    # zero-argument unrelated site, which must not break APIC discovery.
    notes = bytearray()
    for provider, event, location, arguments in (
        ("qemu", "unrelated", 0x401010, ""),
        ("qemu", "apic_register_write", 0x401020, "4@%edi 8@%rsi"),
        ("qemu", "apic_register_write", 0x401030, "4@%ebx 8@%r12"),
        ("other", "apic_register_write", 0x401040, "4@%edi"),
    ):
        desc = struct.pack("<QQQ", location, 0x401000, 0x401060)
        desc += f"{provider}\0{event}\0{arguments}\0".encode()
        notes += struct.pack("<III", 8, len(desc), 3) + b"stapsdt\0"
        notes += desc + b"\0" * (-len(desc) % 4)
    elf = bytearray(512)
    elf[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", elf, 18, 62)
    struct.pack_into("<QQ", elf, 32, 64, 128)
    struct.pack_into("<HHHH", elf, 54, 56, 1, 64, 3)
    struct.pack_into("<IIQQQQQQ", elf, 64, 1, 5, 256, 0x401000, 0, 256, 256, 4096)
    struct.pack_into("<IIQQQQIIQQ", elf, 128, 0, 7, 0, 0, 512, len(notes), 0, 0, 4, 0)
    symbol = struct.pack("<IBBHQQ", 1, 0x12, 0, 1, 0x401040, 16)
    strings = b"\0apic_get_interrupt\0"
    struct.pack_into("<IIQQQQIIQQ", elf, 192, 0, 11, 0, 0, 512 + len(notes), 24, 2, 0, 8, 24)
    struct.pack_into("<IIQQQQIIQQ", elf, 256, 0, 3, 0, 0, 512 + len(notes) + 24, len(strings), 0, 0, 1, 0)
    executable = tmp_path / "test-qemu"
    executable.write_bytes(elf + notes + symbol + strings)
    assert boot_interrupts._apic_probes(executable) == [
        boot_interrupts._Probe("apic_register_write", 288, 352, "4@%edi 8@%rsi"),
        boot_interrupts._Probe("apic_register_write", 304, 352, "4@%ebx 8@%r12"),
        boot_interrupts._Probe("apic_get_interrupt", 320, 0, "", "r"),
    ]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("4@%edi 8@%rsi", "arg1=%di:u32 arg2=%si:u64"),
        ("-4@%r12d 8@-8(%rbp)", "arg1=%r12:s32 arg2=-8(%bp):u64"),
        ("1@%eax 8@(%rsp)", "arg1=%ax:u8 arg2=+0(%sp):u64"),
        ("4@$7", "arg1=\\7:u32"),
        # The pinned launcher's apic_register_write SDT site (QEMU 10.1.0).
        ("1@15(%rsp) 8@208(%rsp)", "arg1=+15(%sp):u8 arg2=+208(%sp):u64"),
    ],
)
def test_sdt_operands_translate_to_kernel_fetchargs(arguments: str, expected: str) -> None:
    assert boot_interrupts._fetch_arguments(arguments) == expected


@pytest.mark.parametrize("arguments", ["4@%xmm0", "4@(%rax,%rdx,4)", "16@%rax"])
def test_unrepresentable_sdt_arguments_fail_explicitly(arguments: str) -> None:
    with pytest.raises(ValueError, match="Unsupported SDT"):
        boot_interrupts._fetch_arguments(arguments)


@pytest.mark.parametrize("anonymous", [False, True])
def test_trace_pipe_retains_late_cpu0_when_cpu1_is_noisy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anonymous: bool
) -> None:
    monkeypatch.setattr(boot_interrupts, "MAX_BYTES", 300)
    monkeypatch.setattr(boot_interrupts, "CPU0_BYTES", 180)
    monkeypatch.setattr(boot_interrupts, "CPU1_BYTES", 60)
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "tracing_on").touch()
    (instance / "events").mkdir()
    (instance / "events" / "enable").touch()
    os.mkfifo(instance / "trace_pipe")
    process = tmp_path / "proc" / "42"
    (process / "task" / "43").mkdir(parents=True)
    (process / "task" / "43" / "comm").write_text("CPU 0/TCG\n")
    (process / "task" / "44").mkdir()
    record: dict[str, Any] = {}
    name = b"<...>" if anonymous else b"CPU 0/TCG"
    before = name + b"-43 [000] ..... 1.0: eoi_before: vector=236\n"
    noise = b"CPU 1/TCG-44 [000] ..... 1.1: apic_local_deliver: vector=100\n"
    after = name + b"-43 [000] ..... 9.0: eoi_after: vector=236\n"

    def produce() -> None:
        with (instance / "trace_pipe").open("wb", buffering=0) as stream:
            stream.write(before + noise * 50 + after)

    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        producing = executor.submit(produce)
        producing.add_done_callback(lambda _future: stop.set())
        boot_interrupts._drain_trace(instance, tmp_path / "interrupts.log", stop, record, process)
        producing.result()
    retained = (tmp_path / "interrupts.log").read_bytes()
    assert before in retained
    assert after in retained
    assert record["bytes_retained"] <= 300
    assert record["overwritten_lines_by_cpu"]["cpu1"] > 0
    assert record["overwritten_lines_by_cpu"]["cpu0"] == 0
    assert "cpu1" in record["coverage_gap"]


def test_trace_reader_excludes_other_launchers_and_accepts_new_threads(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    os.mkfifo(instance / "trace_pipe")
    process = tmp_path / "proc" / "42"
    (process / "task" / "42").mkdir(parents=True)
    record: dict[str, Any] = {}
    stop = threading.Event()
    first = b"qemu-kvm-42 [000] ..... 1.0: apic_register_write_0: arg1=11 arg2=0\n"
    anonymous = b"<...>-42 [000] ..... 1.05: apic_register_write_0: arg1=11 arg2=0\n"
    foreign = b"CPU 0/TCG-99 [000] ..... 1.1: apic_get_interrupt_1: vector=236\n"
    new_thread = b"CPU 1/TCG-43 [000] ..... 1.2: apic_get_interrupt_1: vector=236\n"

    def produce() -> None:
        with (instance / "trace_pipe").open("wb", buffering=0) as stream:
            stream.write(first + anonymous + foreign + b"unrecognized trace record\n")
            (process / "task" / "43").mkdir()
            stream.write(new_thread)
        stop.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        producing = executor.submit(produce)
        boot_interrupts._drain_trace(instance, tmp_path / "interrupts.log", stop, record, process)
        producing.result()
    assert (tmp_path / "interrupts.log").read_bytes() == first + anonymous + new_thread
    assert record["retained_tids"] == [42, 43]
    assert record["other_process_trace_lines"] == 1
    assert record["unrecognized_trace_lines"] == 1
    assert record["unresolved_anonymous_lines"] == 1
    assert "unresolved-thread-names" in record["coverage_gap"]


def test_tracefs_control_write_neither_appends_nor_truncates(tmp_path: Path) -> None:
    # O_TRUNC removes unrelated kernel probes; O_APPEND is rejected by tracefs.
    control = tmp_path / "test-uprobe-events"
    control.write_text("123456")
    boot_interrupts._write_trace(control, "0")
    assert control.read_text() == "0\n3456"


@pytest.mark.parametrize("fail_enable", [False, True])
def test_apic_probes_are_scoped_and_removed_on_setup_or_scenario_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_enable: bool
) -> None:
    trace = tmp_path / "tracefs"
    (trace / "instances").mkdir(parents=True)
    (trace / "uprobe_events").touch()
    (trace / "uprobe_profile").write_text("unrelated-test-process 0\n")
    process = tmp_path / "proc" / "42"
    for tid in ("42", "43"):
        (process / "task" / tid).mkdir(parents=True)
    probes = [
        boot_interrupts._Probe("apic_register_write", 128, 256, "1@15(%rsp) 8@208(%rsp)"),
        boot_interrupts._Probe("apic_get_interrupt", 512, 0, "", "r"),
    ]
    monkeypatch.setattr(boot_interrupts, "_apic_probes", lambda _exe: probes)
    monkeypatch.setattr(boot_interrupts, "_exact_probes", lambda _exe: ([], "test"))
    monkeypatch.setattr(boot_interrupts, "_drain_trace", lambda *_args: None)
    writes = []

    def write(path: Path, value: str) -> None:
        writes.append((path.relative_to(trace), value))
        if fail_enable and path.name == "enable" and value == "1":
            raise OSError("test activation failure")

    monkeypatch.setattr(boot_interrupts, "_write_trace", write)
    record: dict[str, Any] = {"target": {"pod_uid": "test-uid"}, "errors": []}
    with (
        pytest.raises(RuntimeError, match="test scenario failure"),
        boot_interrupts._capture_apic(process, tmp_path, record, trace_root=trace),
    ):
        raise RuntimeError("test scenario failure")
    assert not list((trace / "instances").iterdir())
    commands = [value for path, value in writes if path == Path("uprobe_events")]
    assert len(commands) == 4
    assert all(
        command.startswith(("p:agentplane_boot_testuid/", "r:agentplane_boot_testuid/")) for command in commands[:2]
    )
    assert {command.removeprefix("-:") for command in commands[2:]} == {
        command.split()[0][2:] for command in commands[:2]
    }
    filtering = [(path.name, value) for path, value in writes if path.name in {"set_event_pid", "event-fork"}]
    assert filtering[:2] == [("set_event_pid", "42"), ("event-fork", "1")]
    assert set(filtering[2][1].split()) == {"42", "43"}
    assert any(path.name == "enable" and value == "0" for path, value in writes)
    assert bool(record["errors"]) == fail_enable


def test_userspace_detection_keeps_early_console(tmp_path: Path) -> None:
    console = tmp_path / "container" / "serial.log"
    console.parent.mkdir()
    console.write_bytes(b"[0.0] Linux version\n")
    assert not boot_interrupts._userspace(tmp_path)
    with console.open("ab") as stream:
        stream.write(b"[2.0] systemd[1]: systemd running\n")
    assert boot_interrupts._userspace(tmp_path)
    assert console.read_bytes().startswith(b"[0.0] Linux version\n")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (b"qemu-kvm\0-name\0guest=test-domain,debug-threads=on\0", True),
        (b"qemu-kvm\0-name\0test-domain\0", True),
        (b"qemu-kvm\0-name\0guest=test-domain-other\0", False),
        (b"qemu-kvm\0-machine\0none\0", False),
        (b"qemu-pr-helper\0", False),
    ],
)
def test_only_the_named_guest_process_can_start_capture(tmp_path: Path, arguments: bytes, expected: bool) -> None:
    (tmp_path / "cmdline").write_bytes(arguments)
    assert boot_interrupts._domain_process(tmp_path, "test-domain") == expected
    assert not boot_interrupts._domain_process(tmp_path / "exited", "test-domain")


def test_missing_executable_preserves_monitor_timeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot_interrupts, "_domain_process", lambda *_args: True)
    monkeypatch.setattr(
        boot_interrupts.boot_rcu, "_sample_qemu", lambda *_args: {"matching_qemu_processes": 1, "pid": 42}
    )
    monkeypatch.setattr(boot_interrupts, "MAX_SAMPLES", 2)
    monkeypatch.setattr(boot_interrupts, "SAMPLE_SECONDS", 0)

    def qmp(*args: Any) -> Any:
        command = args[-1]
        if command["execute"] == "query-status":
            return {"running": True}
        assert command["execute"] == "human-monitor-command"
        assert command["arguments"]["command-line"].startswith("info ")
        return "read-only monitor sample"

    monkeypatch.setattr(boot_interrupts, "_qmp", qmp)
    target = {"pod_name": "test-launcher", "pod_uid": "test-uid", "vmi_name": "test-guest"}
    boot_interrupts._trace_one(
        lambda *_args: "", "test-scope", target, tmp_path, threading.Event(), proc_root=tmp_path / "gone-proc"
    )
    output = tmp_path / "boot-interrupts" / "test-uid"
    summary = json.loads((output / "summary.json").read_text())
    samples = [json.loads(line) for line in (output / "timeline.jsonl").read_text().splitlines()]
    assert len(samples) == 2
    assert all(
        [(p["command"], p["cpu"]) for p in sample["probes"]]
        == [("info lapic", 0), ("info lapic", 1), ("info pic", None)]
        for sample in samples
    )
    assert summary["stop_reason"] == "sample-limit"
    assert summary["accepted_events"] == []
    assert len(summary["errors"]) == 1


def test_apic_capture_precedes_monitor_and_finishes_at_userspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boot_interrupts, "_domain_process", lambda *_args: True)
    monkeypatch.setattr(
        boot_interrupts.boot_rcu, "_sample_qemu", lambda *_args: {"matching_qemu_processes": 1, "pid": 42}
    )
    monkeypatch.setattr(boot_interrupts, "_userspace", lambda _launcher: True)
    order = []

    @contextmanager
    def apic(process: Path, output: Path, record: dict[str, Any]) -> Iterator[None]:
        order.append("attach")
        try:
            yield
        finally:
            order.append("detach")
            record["trace_stats"] = {"cpu0": "overrun: 0"}

    def qmp(*args: Any) -> Any:
        order.append(args[-1]["execute"])
        return {"running": True} if args[-1]["execute"] == "query-status" else "test sample"

    monkeypatch.setattr(boot_interrupts, "_capture_apic", apic)
    monkeypatch.setattr(boot_interrupts, "_qmp", qmp)
    target = {"pod_name": "test-launcher", "pod_uid": "test-uid", "vmi_name": "test-guest"}
    boot_interrupts._trace_one(lambda *_args: "", "test-scope", target, tmp_path, threading.Event())
    summary = json.loads((tmp_path / "boot-interrupts" / "test-uid" / "summary.json").read_text())
    assert order[0:2] == ["attach", "query-status"]
    assert order[-1] == "detach"
    assert summary["stop_reason"] == "userspace"
    assert summary["samples"] == 1
    assert summary["trace_stats"] == {"cpu0": "overrun: 0"}


@pytest.mark.parametrize("helper_present", [False, True])
def test_pending_or_helper_launcher_does_not_spend_observer_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper_present: bool
) -> None:
    checking = threading.Event()
    qemu_ready = threading.Event()
    tracing = threading.Event()
    target = {"pod_name": "test-launcher", "pod_uid": "test-uid", "vmi_name": "test-guest"}
    monkeypatch.setattr(boot_interrupts.boot_rcu, "_namespace_launchers", lambda *_args: ([target], []))

    def hosts(_root: Path, uids: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        checking.set()
        return {
            uid: {"matching_qemu_processes": int(helper_present or qemu_ready.is_set()), "pid": 42} for uid in uids
        }, {}

    def trace(*_args: Any) -> None:
        assert qemu_ready.is_set()
        tracing.set()

    monkeypatch.setattr(boot_interrupts.boot_rcu, "_sample_qemus", hosts)
    monkeypatch.setattr(boot_interrupts, "_domain_process", lambda *_args: qemu_ready.is_set())
    monkeypatch.setattr(boot_interrupts, "_trace_one", trace)
    with boot_interrupts.capture(lambda *_args: "", "test-scope", tmp_path):
        assert checking.wait(5)
        assert not tracing.is_set()
        qemu_ready.set()
        assert tracing.wait(5)
    assert json.loads((tmp_path / "boot-interrupts" / "observer.json").read_text()) == {
        "pod_uids": ["test-uid"],
        "errors": [],
    }
