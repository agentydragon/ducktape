from __future__ import annotations

import pytest_bazel
from fastmcp.client import Client

from mcp_infra.exec.direct import DirectExecServer
from mcp_infra.exec.models import Exited
from mcp_infra.exec.subprocess import DirectExecArgs
from mcp_infra.testing.exec_stubs import DirectExecServerStub


async def test_direct_exec_echo_inproc() -> None:
    """Direct exec (unsandboxed) in-proc server echo test."""

    server = DirectExecServer()
    async with Client(server) as session:
        stub = DirectExecServerStub.from_server(server, session)
        res = await stub.exec(DirectExecArgs(cmd=["/bin/echo", "hello"], max_bytes=100000, timeout_ms=5000))
        assert res.exit == Exited(exit_code=0)
        assert res.stdout == "hello\n"


def test_direct_exec_omits_env_knob() -> None:
    """The agent-facing exec schema must NOT expose `env` / `inherit_env`.

    glm-4.6 (critic run 824e8815) emitted the optional `env` arg as a JSON-encoded
    string ("null" / "[]" / '["PATH=..."]') under strict tool-calling, which the
    `list[EnvVar] | None` field rejected with "Input should be a valid list" —
    failing 100% of exec calls. The knob was removed (direct exec inherits the
    ambient env), so models can't trip on it.
    See props/debug/glm46_exec_env_stringification.md.
    """
    props = DirectExecArgs.model_json_schema()["properties"]
    assert "env" not in props
    assert "inherit_env" not in props


if __name__ == "__main__":
    pytest_bazel.main()
