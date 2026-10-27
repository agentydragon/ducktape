from __future__ import annotations

import pytest_bazel
from fastmcp.client import Client

from util.exec.models import BaseExecResult, Exited
from x.ember.direct_exec import DirectExecServer


async def test_direct_exec_echo_inproc() -> None:
    """Direct exec (unsandboxed) in-proc server echo test."""

    server = DirectExecServer()
    async with Client(server) as session:
        response = await session.call_tool(
            "exec", {"cmd": ["/bin/echo", "hello"], "max_bytes": 100000, "timeout_ms": 5000, "cwd": None}
        )
        res = BaseExecResult.model_validate(response.structured_content)
        assert res.exit == Exited(exit_code=0)
        assert res.stdout == "hello\n"


if __name__ == "__main__":
    pytest_bazel.main()
