from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.model import SBPLPolicy
from mcp_infra.seatbelt.runner import run_sandboxed_async

pytestmark = [*REQUIRES_SANDBOX_EXEC]

# policy_deny_users fixture is provided by conftest.py


async def test_exec_allow_root_deny_users(policy_deny_users: SBPLPolicy):
    ok = await run_sandboxed_async(policy_deny_users, ["/bin/sh", "-c", "ls /System"])
    assert ok.exit_code == 0

    deny = await run_sandboxed_async(policy_deny_users, ["/bin/sh", "-c", "ls /Users"])
    assert deny.exit_code != 0
