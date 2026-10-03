"""The harness supervisor's side of the contract with `HarnessProcess.start`."""

import asyncio
import os
import signal
import socket
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path, own_repo_rlocation


async def test_the_native_pid_report_is_one_write() -> None:
    """The runner reads the report once and closes its end, so a report split across writes fails
    the supervisor's second write with EPIPE. A packet socket keeps each write a separate message."""
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        supervisor = await asyncio.create_subprocess_exec(
            get_required_path(own_repo_rlocation("agentplane/runner/harness_supervisor")),
            "--native-pid-fd",
            str(writer.fileno()),
            "/bin/true",
            pass_fds=(writer.fileno(),),
            start_new_session=True,
        )
        writer.close()
        report = await asyncio.to_thread(reader.recv, 32)
        assert await supervisor.wait() == 0
    assert report.endswith(b"\n")
    assert int(report) > 0


async def test_supervisor_signals_the_native_process_after_dropping_its_uid(tmp_path: Path) -> None:
    """A guest stop must reach the unprivileged native process through the supervisor."""
    assert os.geteuid() == 0, "cross-UID supervisor coverage requires the privileged BuildBuddy worker"
    cgroup_procs = tmp_path / "cgroup.procs"
    cgroup_kill = tmp_path / "cgroup.kill"
    cgroup_procs.touch()
    cgroup_kill.touch()
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        supervisor = await asyncio.create_subprocess_exec(
            get_required_path(own_repo_rlocation("agentplane/runner/harness_supervisor")),
            "--native-pid-fd",
            str(writer.fileno()),
            "--cgroup-procs",
            str(cgroup_procs),
            "--cgroup-kill",
            str(cgroup_kill),
            "--agent-uid",
            "65534",
            "--agent-gid",
            "65534",
            "/bin/sleep",
            "30",
            pass_fds=(writer.fileno(),),
            start_new_session=True,
        )
        writer.close()
        report = await asyncio.to_thread(reader.recv, 32)
        native_pid = int(report)
        assert cgroup_procs.read_text() == f"{native_pid}\n"
        supervisor.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(supervisor.wait(), timeout=5) == 128 + signal.SIGTERM
    assert cgroup_kill.read_text() == "1\n"


if __name__ == "__main__":
    pytest_bazel.main()
