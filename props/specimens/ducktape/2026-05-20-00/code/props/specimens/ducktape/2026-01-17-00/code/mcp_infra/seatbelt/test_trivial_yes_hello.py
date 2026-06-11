from __future__ import annotations

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.runner import run_sandboxed_async

pytestmark = [*REQUIRES_SANDBOX_EXEC]


async def test_trivial_yes_hello_world(allow_all_policy):
    res = await run_sandboxed_async(allow_all_policy, ["/bin/sh", "-c", "yes hello | head -n 5"])
    assert res.exit_code == 0
    stdout_bytes = res.stdout if res.stdout is not None else b""
    stderr_bytes = res.stderr if res.stderr is not None else b""
    assert b"hello" in stdout_bytes or b"hello" in stderr_bytes
