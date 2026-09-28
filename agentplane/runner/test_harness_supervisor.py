"""The harness supervisor's side of the contract with `HarnessProcess.start`."""

import asyncio
import socket

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


if __name__ == "__main__":
    pytest_bazel.main()
