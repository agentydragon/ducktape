from __future__ import annotations

import sys

from adgn.seatbelt.runner import run_sandboxed_async
from tests._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC]


async def test_python_print_ok(allow_all_policy):
    res = await run_sandboxed_async(allow_all_policy, [sys.executable, "-c", "print('PYOK')"])
    assert (res.exit_code, res.stdout) == (0, b"PYOK\n")
