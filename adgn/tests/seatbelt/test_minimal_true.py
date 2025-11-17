from __future__ import annotations

from adgn.seatbelt.runner import run_sandboxed_async
from tests._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC]


async def test_minimal_true_exits_zero(restrictive_echo_policy):
    # Minimal restrictive policy should be sufficient for /usr/bin/true
    res = await run_sandboxed_async(restrictive_echo_policy, ["/usr/bin/true"])
    assert res.exit_code == 0
