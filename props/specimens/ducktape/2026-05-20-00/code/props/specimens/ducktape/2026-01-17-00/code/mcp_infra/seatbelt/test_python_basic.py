from __future__ import annotations

import sys

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.runner import run_sandboxed_async

pytestmark = [*REQUIRES_SANDBOX_EXEC]


async def test_python_print_ok(allow_all_policy):
    res = await run_sandboxed_async(allow_all_policy, [sys.executable, "-c", "print('PYOK')"])
    assert (res.exit_code, res.stdout) == (0, b"PYOK\n")
