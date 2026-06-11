from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.model import DefaultBehavior, FileOp, FileRule, ProcessRule, SBPLPolicy, Subpath
from mcp_infra.seatbelt.runner import apopen, run_sandboxed_async

pytestmark = [*REQUIRES_SANDBOX_EXEC]


async def test_apopen_interactive_echo_with_trace(tmp_path: Path):
    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec not found on PATH")

    # Allow reading/writing broadly enough for echo/cat
    policy = SBPLPolicy(
        default_behavior=DefaultBehavior.ALLOW,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
            FileRule(op=FileOp.FILE_WRITE_STAR, filters=[Subpath(subpath="/")]),
        ],
    )

    p = await apopen(["/bin/sh", "-c", "cat"], policy, trace=True)
    # Trace path should be available immediately
    assert p.trace_file is not None
    # Write and read a line
    assert p.stdin is not None
    assert p.stdout is not None
    p.stdin.write(b"hello\n")
    await p.stdin.drain()
    line = await asyncio.wait_for(p.stdout.readline(), timeout=2)
    assert line == b"hello\n"
    p.stdin.close()
    await asyncio.wait_for(p.wait(), timeout=5)
    p.cleanup()


async def test_run_sandboxed_async_echo(tmp_path: Path):
    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec not found on PATH")

    policy = SBPLPolicy(
        default_behavior=DefaultBehavior.ALLOW,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
            FileRule(op=FileOp.FILE_WRITE_STAR, filters=[Subpath(subpath="/")]),
        ],
    )

    res = await run_sandboxed_async(
        policy, ["/bin/echo", "OK"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, trace=True
    )
    assert res.exit_code == 0
    assert res.stdout == b"OK\n"
    # Trace managed internally and captured
    assert res.trace_path is not None
    assert res.trace_text is None or isinstance(res.trace_text, str)
