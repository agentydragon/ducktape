"""Boot diagnostics survive missing events and preserve a temporal APIC record."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.vm import boot_interrupts


def test_changed_pod_uid_prevents_monitor_command() -> None:
    calls = []

    def kubectl(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        return json.dumps({"metadata": {"uid": "replacement"}})

    with pytest.raises(RuntimeError, match="UID changed"):
        boot_interrupts._qmp(kubectl, "scope", "launcher", "original", "domain", {"execute": "query-status"})
    assert len(calls) == 1


@pytest.mark.parametrize("events_available", [True, False])
@pytest.mark.parametrize("lost_enable_reply", [True, False])
def test_missing_events_still_sample_and_uncertain_activation_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events_available: bool, lost_enable_reply: bool
) -> None:
    process = tmp_path / "proc" / "42"
    (process / "root" / "tmp").mkdir(parents=True)
    (process / "cmdline").write_bytes(b"qemu\0-accel\0tcg\0")
    monkeypatch.setattr(
        boot_interrupts.boot_rcu, "_sample_qemu", lambda *_args: {"matching_qemu_processes": 1, "pid": 42}
    )
    monkeypatch.setattr(boot_interrupts, "MAX_SAMPLES", 2)
    monkeypatch.setattr(boot_interrupts, "SAMPLE_SECONDS", 0)
    monkeypatch.setattr(boot_interrupts, "MAX_BYTES", 40)
    calls: list[dict[str, Any]] = []

    def qmp(*args: Any) -> Any:
        command = args[-1]
        calls.append(command)
        match command["execute"]:
            case "query-status":
                return {"running": True}
            case "trace-event-get-state":
                return [{"name": "apic_local_deliver", "state": "disabled"}] if events_available else []
            case "trace-event-set-state":
                if lost_enable_reply and command["arguments"]["enable"]:
                    raise RuntimeError("activation reply lost")
                return {}
            case "human-monitor-command":
                hmp = command["arguments"]["command-line"]
                if hmp.startswith("logfile /tmp/"):
                    (process / "root" / hmp.removeprefix("logfile /")).write_bytes(
                        b"Servicing hardware INT=0xec\n" * 10
                    )
                if hmp.startswith("info "):
                    return f"APIC sample {len(calls)}"
                return ""
        raise AssertionError(command)

    monkeypatch.setattr(boot_interrupts, "_qmp", qmp)
    target = {"pod_name": "launcher", "pod_uid": "uid", "vmi_name": "guest"}
    boot_interrupts._trace_one(
        lambda *_args: "", "scope", target, tmp_path, threading.Event(), proc_root=tmp_path / "proc"
    )
    output = tmp_path / "boot-interrupts" / "uid"
    summary = json.loads((output / "summary.json").read_text())
    samples = [json.loads(line) for line in (output / "timeline.jsonl").read_text().splitlines()]
    assert len(samples) == 2
    assert samples[0]["started_at"] <= samples[1]["started_at"]
    for sample in samples:
        assert [(p["command"], p["cpu"]) for p in sample["probes"]] == [
            ("info lapic", 0),
            ("info lapic", 1),
            ("info pic", None),
        ]
    assert samples[0]["probes"] != samples[1]["probes"]
    assert summary["accepted_events"] == (["apic_local_deliver"] if events_available and not lost_enable_reply else [])
    assert summary["log_int_accepted"]
    assert (output / "interrupts.log").stat().st_size == 40
    assert "logging_stopped_at_byte_limit" in summary
    disabled = [
        c["arguments"]["name"]
        for c in calls
        if c["execute"] == "trace-event-set-state" and not c["arguments"]["enable"]
    ]
    assert disabled == (["apic_local_deliver"] if events_available else [])
    assert calls[-1]["arguments"]["command-line"] == "logfile /proc/self/fd/2"


def test_userspace_detection_keeps_early_console(tmp_path: Path) -> None:
    console = tmp_path / "container" / "serial.log"
    console.parent.mkdir()
    console.write_bytes(b"[0.0] Linux version\n")
    assert not boot_interrupts._userspace(tmp_path)
    with console.open("ab") as stream:
        stream.write(b"[2.0] systemd[1]: systemd running\n")
    assert boot_interrupts._userspace(tmp_path)
    assert console.read_bytes().startswith(b"[0.0] Linux version\n")


def test_host_pid_disappearance_does_not_discard_monitor_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        boot_interrupts.boot_rcu, "_sample_qemu", lambda *_args: {"matching_qemu_processes": 1, "pid": 42}
    )
    monkeypatch.setattr(boot_interrupts, "MAX_SAMPLES", 2)
    monkeypatch.setattr(boot_interrupts, "SAMPLE_SECONDS", 0)
    calls: list[dict[str, Any]] = []

    def qmp(*args: Any) -> Any:
        command = args[-1]
        calls.append(command)
        if command["execute"] == "query-status":
            return {"running": True}
        if command["execute"] == "trace-event-get-state":
            return []
        assert command["execute"] == "human-monitor-command"
        assert command["arguments"]["command-line"].startswith("info ")
        return "read-only monitor sample"

    monkeypatch.setattr(boot_interrupts, "_qmp", qmp)
    target = {"pod_name": "launcher", "pod_uid": "uid", "vmi_name": "guest"}
    boot_interrupts._trace_one(
        lambda *_args: "", "scope", target, tmp_path, threading.Event(), proc_root=tmp_path / "gone-proc"
    )
    output = tmp_path / "boot-interrupts" / "uid"
    summary = json.loads((output / "summary.json").read_text())
    samples = [json.loads(line) for line in (output / "timeline.jsonl").read_text().splitlines()]
    assert len(samples) == 2
    assert all(len(sample["probes"]) == 3 for sample in samples)
    assert summary["stop_reason"] == "sample-limit"
    assert "Host process unavailable" in summary["logging_skipped"]
    assert summary["errors"] == []
    assert "log_int_accepted" not in summary


if __name__ == "__main__":
    pytest_bazel.main()
