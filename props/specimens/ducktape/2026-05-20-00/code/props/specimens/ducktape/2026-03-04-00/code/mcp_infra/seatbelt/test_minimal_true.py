from __future__ import annotations

import pytest_bazel

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.runner import run_sandboxed_async

pytestmark = [*REQUIRES_SANDBOX_EXEC]


async def test_minimal_true_exits_zero(restrictive_echo_policy):
    # Minimal restrictive policy should be sufficient for /usr/bin/true
    res = await run_sandboxed_async(restrictive_echo_policy, ["/usr/bin/true"])
    assert res.exit_code == 0


if __name__ == "__main__":
    pytest_bazel.main()
