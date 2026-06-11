from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastmcp.client import Client

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.exec.models import Exited, TimedOut
from mcp_infra.exec.seatbelt import SandboxExecArgs, SeatbeltExecServer
from mcp_infra.seatbelt.model import (
    DefaultBehavior,
    FileOp,
    FileRule,
    MachLookupRule,
    ProcessRule,
    SBPLPolicy,
    Subpath,
    SystemRule,
    TraceConfig,
)
from mcp_infra.testing.exec_stubs import SeatbeltExecServerStub

pytestmark = [*REQUIRES_SANDBOX_EXEC, pytest.mark.shell]


@pytest.fixture
async def seatbelt_session():
    """Yield (server, Client session) for sandbox exec tests."""
    server = SeatbeltExecServer()
    async with Client(server) as sess:
        yield server, sess


def make_default_restrictive_policy(trace: bool = False) -> SBPLPolicy:
    return SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
        ],
        network=[],
        mach=MachLookupRule(global_names=[]),
        system=SystemRule(system_socket=False, sysctl_read=False),
        trace=TraceConfig(enabled=trace, path=None),
    )


def _extract_payload(resp):
    # Prefer structured_content (FastMCP dataclass) else unwrap result
    sc = getattr(resp, "structured_content", None)
    if sc is not None:
        return sc
    r = getattr(resp, "result", None)
    if r is not None:
        if isinstance(r, dict) and set(r.keys()) == {"result"}:
            return r["result"]
        return r
    if isinstance(resp, dict) and set(resp.keys()) == {"result"}:
        return resp["result"]
    return resp


async def test_sandbox_exec_echo_roundtrip(seatbelt_session) -> None:
    _server, session = seatbelt_session
    # Execute echo under sandbox (typed stub)
    stub = SeatbeltExecServerStub.from_server(_server, session)
    res = await stub.sandbox_exec(
        SandboxExecArgs(
            policy=make_default_restrictive_policy(trace=False),
            argv=["/bin/echo", "HELLO_MINIMAL"],
            max_bytes=100000,
            timeout_ms=10_000,
            trace=False,
        )
    )
    assert isinstance(res.exit, Exited)
    assert res.exit.exit_code == 0
    assert isinstance(res.stdout, str)  # Short output should not be truncated
    assert res.stdout == "HELLO_MINIMAL\n"
    # stderr should be empty or None
    assert isinstance(res.stderr, str)  # Short error should not be truncated
    assert res.stderr in ("", None)
    # duration exists and is a non-negative int
    assert isinstance(res.duration_ms, int)
    assert res.duration_ms >= 0


async def test_sandbox_exec_write_denied(seatbelt_session) -> None:
    """Attempt a file write that should be denied by the sandbox policy."""
    _server, session = seatbelt_session
    # Attempt to write to /tmp (normally allowed for a user; should be denied by sandbox)
    token = secrets.token_hex(6)
    out_path = f"/tmp/seatbelt_denied_{token}.txt"
    stub = SeatbeltExecServerStub.from_server(_server, session)
    res = await stub.sandbox_exec(
        SandboxExecArgs(
            policy=make_default_restrictive_policy(trace=True),
            argv=["/bin/sh", "-lc", f"echo DENIED > {out_path}"],
            max_bytes=100000,
            timeout_ms=5_000,
            trace=True,
        )
    )
    assert isinstance(res.exit, Exited)
    # Expect non-zero exit due to sandbox denial
    assert isinstance(res.exit.exit_code, int)
    assert res.exit.exit_code != 0
    # Stderr should have some diagnostic
    assert isinstance(res.stderr, str)  # Short error should not be truncated
    assert res.stderr != ""
    # File should not exist (write was denied)
    assert not Path(out_path).exists()
    # Trace collection remains flaky across versions; rely on stderr for now
    # TODO(mpokorny): Revisit trace enablement and policy for reliable capture


async def test_sandbox_exec_timeout(seatbelt_session) -> None:
    """Command exceeding timeout should return timeout=True and no exit_code."""
    _server, session = seatbelt_session
    policy = make_default_restrictive_policy()
    stub = SeatbeltExecServerStub.from_server(_server, session)
    res = await stub.sandbox_exec(
        SandboxExecArgs(
            policy=policy, argv=["/bin/sh", "-lc", "sleep 2"], max_bytes=100000, timeout_ms=500, trace=False
        )
    )
    assert isinstance(res.exit, TimedOut)
    assert isinstance(res.duration_ms, int)
    assert res.duration_ms >= 0


async def test_sandbox_exec_cwd_and_env(tmp_path: Path, seatbelt_session) -> None:
    """Verify cwd and env injection (async)."""
    _server, session = seatbelt_session
    policy = make_default_restrictive_policy()
    stub = SeatbeltExecServerStub.from_server(_server, session)
    res = await stub.sandbox_exec(
        SandboxExecArgs(
            policy=policy,
            argv=["/bin/sh", "-lc", "pwd; echo $FOO"],
            cwd=str(tmp_path),
            env={"FOO": "BAR"},
            max_bytes=100000,
            timeout_ms=5_000,
            trace=False,
        )
    )
    assert isinstance(res.exit, Exited)
    assert res.exit.exit_code == 0
    assert isinstance(res.stdout, str)  # Short output should not be truncated
    assert res.stdout.splitlines()[:2] == [str(tmp_path), "BAR"]

    # No policy CRUD tests; server no longer stores policies.
