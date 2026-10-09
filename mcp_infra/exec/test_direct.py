from __future__ import annotations

import pytest_bazel
from fastmcp.client import Client

from mcp_infra.exec.direct import DirectExecServer
from mcp_infra.testing.exec_stubs import DirectExecServerStub
from util.exec.models import Exited
from util.exec.subprocess import DirectExecArgs


async def test_direct_exec_echo_inproc() -> None:
    """Direct exec (unsandboxed) in-proc server echo test."""

    server = DirectExecServer()
    async with Client(server) as session:
        stub = DirectExecServerStub.from_server(server, session)
        res = await stub.exec(DirectExecArgs(cmd=["/bin/echo", "hello"], max_bytes=100000, timeout_ms=5000))
        assert res.exit == Exited(exit_code=0)
        assert res.stdout == "hello\n"


if __name__ == "__main__":
    pytest_bazel.main()
