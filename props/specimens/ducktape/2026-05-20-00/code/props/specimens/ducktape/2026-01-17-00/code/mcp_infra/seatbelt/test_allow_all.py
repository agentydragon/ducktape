from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.model import SBPLPolicy
from mcp_infra.seatbelt.runner import run_sandboxed_async

pytestmark = [*REQUIRES_SANDBOX_EXEC]

# allow_all_policy fixture is provided by conftest.py


async def test_exec_allow_all_runs_echo(allow_all_policy: SBPLPolicy):
    res = await run_sandboxed_async(allow_all_policy, ["/bin/sh", "-c", "echo ALLOW_ALL_OK"])
    assert res.exit_code == 0
    assert res.stdout == b"ALLOW_ALL_OK\n"
