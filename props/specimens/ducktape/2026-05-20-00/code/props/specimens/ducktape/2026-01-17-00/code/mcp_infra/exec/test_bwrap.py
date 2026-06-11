from __future__ import annotations

import shutil
import sys

import pytest
from fastmcp.client import Client

from mcp_infra.exec.bwrap import BwrapExecArgs, BwrapExecServer
from mcp_infra.exec.models import Exited
from mcp_infra.testing.exec_stubs import BwrapExecServerStub


@pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap is Linux-only")
async def test_bwrap_exec_echo_inproc_linux() -> None:
    """bwrap exec (Linux sandbox) in-proc server echo test."""
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) not found in PATH")

    server = BwrapExecServer()
    async with Client(server) as session:
        stub = BwrapExecServerStub.from_server(server, session)
        res = await stub.exec(BwrapExecArgs(cmd=["/bin/echo", "hello"], max_bytes=100000, timeout_ms=5000))
        assert res.exit == Exited(exit_code=0)
        assert res.stdout == "hello\n"
