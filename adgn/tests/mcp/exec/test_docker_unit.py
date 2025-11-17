from __future__ import annotations

import pytest
from tests.conftest import make_container_opts

from adgn.mcp.exec.docker.server import make_container_exec_server
from adgn.mcp.exec.models import ExecInput, Exited, TimedOut


def _make_server(ephemeral: bool):
    return make_container_exec_server(make_container_opts("alpine:3.19", ephemeral=ephemeral))


@pytest.mark.requires_docker
async def test_ephemeral_exec_stdout_stderr_timeout(make_typed_mcp) -> None:
    server = _make_server(ephemeral=True)

    async with make_typed_mcp(server, "docker") as (client, _session):
        # stdout
        r1 = await client.exec(ExecInput(cmd=["/bin/echo", "hello"], timeout_ms=5000))
        assert r1.exit == Exited(exit_code=0)
        assert isinstance(r1.stdout, str)  # Short output should not be truncated
        assert r1.stdout == "hello\n"
        # stderr and nonzero exit
        r2 = await client.exec(ExecInput(cmd=["sh", "-lc", "echo err 1>&2; exit 3"], timeout_ms=5000))
        assert r2.exit == Exited(exit_code=3)
        assert "err" in (r2.stderr or "")
        # timeout
        r3 = await client.exec(ExecInput(cmd=["sh", "-lc", "sleep 5"], timeout_ms=500))
        assert r3.exit == TimedOut()


@pytest.mark.requires_docker
async def test_persession_exec_timeout_then_next_ok(make_typed_mcp) -> None:
    server = _make_server(ephemeral=False)

    async with make_typed_mcp(server, "docker") as (client, _session):
        # Force timeout
        t1 = await client.exec(ExecInput(cmd=["sh", "-lc", "sleep 3"], timeout_ms=500))
        assert t1.exit == TimedOut()
        # Next call should succeed after restart
        r1 = await client.exec(ExecInput(cmd=["/bin/echo", "ok"], timeout_ms=5000))
        assert r1.exit == Exited(exit_code=0)
        assert isinstance(r1.stdout, str)  # Short output should not be truncated
        assert r1.stdout == "ok\n"
