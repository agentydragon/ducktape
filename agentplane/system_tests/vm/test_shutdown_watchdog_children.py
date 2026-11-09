"""Tests for the guest watchdog's bounded proof-helper process-tree snapshot."""

from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed


def _write_process(
    proc_root: Path, pid: int, comm: str, *, cmdline: bytes = b"", children: list[int] | None = None
) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True, exist_ok=True)
    (process / "comm").write_text(f"{comm}\n")
    (process / "cmdline").write_bytes(cmdline)
    for field, value in (("wchan", b"wait_for_exit"), ("syscall", b"42 0 0"), ("stack", b"[<0>] test_stack")):
        (process / field).write_bytes(value + b"\n")
    task = process / "task" / str(pid)
    task.mkdir(parents=True, exist_ok=True)
    (task / "children").write_text(" ".join(str(child) for child in children or []) + "\n")


def _run_scan(tmp_path: Path, direct_children: list[int]) -> str:
    proc_root = tmp_path / "proc"
    _write_process(proc_root, 1, "systemd", children=direct_children)
    shim = tmp_path / "busybox"
    shim.write_text('#!/bin/sh\ncommand=$1\nshift\nexec "$command" "$@"\n')
    shim.chmod(0o755)
    script_rlocation = os.environ["SHUTDOWN_WATCHDOG_CHILD_SCAN_RLOCATION"]
    script = Runfiles.CreateOrRaise().Rlocation(script_rlocation)
    result = subprocess.run(
        ["/bin/sh", str(script), str(proc_root), str(shim)], check=True, capture_output=True, text=True
    )
    return result.stdout


def test_watchdog_finds_findmnt_two_levels_below_pid1_without_logging_arguments(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    _write_process(proc_root, 1, "systemd", children=[41])
    _write_process(
        proc_root,
        41,
        "agentplane-shut",
        cmdline=b"/usr/sbin/agentplane-shutdown-complete\0private-token-must-not-appear\0",
        children=[42],
    )
    _write_process(proc_root, 42, "helper", children=[43])
    _write_process(proc_root, 43, "findmnt")

    shim = tmp_path / "busybox"
    shim.write_text('#!/bin/sh\ncommand=$1\nshift\nexec "$command" "$@"\n')
    shim.chmod(0o755)
    script = Runfiles.CreateOrRaise().Rlocation(os.environ["SHUTDOWN_WATCHDOG_CHILD_SCAN_RLOCATION"])
    result = subprocess.run(
        ["/bin/sh", str(script), str(proc_root), str(shim)], check=True, capture_output=True, text=True
    )

    assert "pid=41 comm=agentplane-shut role=shutdown-proof" in result.stdout
    assert "pid=43 comm=findmnt role=findmnt" in result.stdout
    assert "nodes=3 discovered=3 findmnt_snapshots=1 complete=1" in result.stdout
    assert "private-token-must-not-appear" not in result.stdout


def test_watchdog_discovers_a_live_shebang_script_using_linux_comm(tmp_path: Path) -> None:
    proof_script = tmp_path / "agentplane-shutdown-complete"
    proof_script.write_text("#!/bin/sh\nsleep 30\n")
    proof_script.chmod(0o755)
    process = subprocess.Popen([str(proof_script)], start_new_session=True)
    try:
        # The kernel fills comm and cmdline once the exec completes.
        for attempt in Retrying(
            stop=stop_after_delay(2), wait=wait_fixed(0.01), retry=retry_if_exception_type(AssertionError), reraise=True
        ):
            with attempt:
                comm = Path(f"/proc/{process.pid}/comm").read_text().strip()
                cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes()
                assert comm
                assert cmdline
        assert comm.startswith("agentplane-shut")
        assert b"agentplane-shutdown-complete" in cmdline

        proc_root = tmp_path / "proc"
        _write_process(proc_root, 1, "systemd", children=[process.pid])
        _write_process(proc_root, process.pid, comm, cmdline=cmdline)
        output = _run_scan(tmp_path, [process.pid])
        assert f"pid={process.pid} comm={comm} role=shutdown-proof" in output
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)


def test_watchdog_marks_a_truncated_proof_process_tree_incomplete(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    _write_process(proc_root, 1, "systemd", children=[41])
    _write_process(proc_root, 41, "sh", cmdline=b"agentplane-shutdown-complete\0", children=[42])
    for pid in range(42, 107):
        children = [pid + 1] if pid < 106 else []
        _write_process(proc_root, pid, "helper", children=children)

    output = _run_scan(tmp_path, [41])

    assert "AGENTPLANE_SHUTDOWN_PROOF_DESCENDANT_SCAN_INCOMPLETE pid=41 max_nodes=64" in output
    assert "findmnt_snapshots=0 complete=0" in output


def test_watchdog_counts_only_findmnt_processes_with_recorded_snapshots(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    _write_process(proc_root, 1, "systemd", children=[41])
    _write_process(proc_root, 41, "sh", cmdline=b"agentplane-shutdown-complete\0", children=list(range(42, 58)))
    for pid in range(42, 58):
        _write_process(proc_root, pid, "findmnt")

    output = _run_scan(tmp_path, [41])

    assert "AGENTPLANE_SHUTDOWN_PID1_CHILDREN_TRUNCATED max=16 omitted=1" in output
    assert "AGENTPLANE_SHUTDOWN_PROOF_DESCENDANT_SCAN_INCOMPLETE pid=41 max_nodes=64" in output
    assert "findmnt_snapshots=15 complete=0" in output
    assert output.count("role=findmnt") == 15


if __name__ == "__main__":
    pytest_bazel.main()
