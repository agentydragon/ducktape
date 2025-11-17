from __future__ import annotations

from fastmcp.client import Client

from adgn.mcp.exec.direct import DirectExecArgs, make_direct_exec_server
from adgn.mcp.exec.models import Exited
from adgn.mcp.testing.exec_stubs import DirectExecServerStub


async def test_direct_exec_echo_inproc() -> None:
    """Direct exec (unsandboxed) in-proc server echo test."""

    server = make_direct_exec_server("exec")
    async with Client(server) as session:
        stub = DirectExecServerStub.from_server(server, session)
        res = await stub.exec(DirectExecArgs(cmd=["/bin/echo", "hello"], max_bytes=100000, timeout_ms=5000))
        assert res.exit == Exited(exit_code=0)
        assert res.stdout == "hello\n"
