import pytest

from adgn.seatbelt.model import Action, DefaultBehavior, FileOp, FileRule, ProcessRule, SBPLPolicy, Subpath
from adgn.seatbelt.runner import run_sandboxed_async
from tests._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC]


@pytest.fixture
def policy_deny_users() -> SBPLPolicy:
    base = SBPLPolicy(
        default_behavior=DefaultBehavior.ALLOW,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
            FileRule(op=FileOp.FILE_WRITE_STAR, filters=[Subpath(subpath="/")]),
        ],
    )
    # Carve-out deny /Users
    base.files += [
        FileRule(op=FileOp.FILE_READ_STAR, action=Action.DENY, filters=[Subpath(subpath="/Users")]),
        FileRule(op=FileOp.FILE_READ_METADATA, action=Action.DENY, filters=[Subpath(subpath="/Users")]),
    ]
    return base


async def test_exec_allow_root_deny_users(policy_deny_users: SBPLPolicy):
    ok = await run_sandboxed_async(policy_deny_users, ["/bin/sh", "-c", "ls /System"])
    assert ok.exit_code == 0

    deny = await run_sandboxed_async(policy_deny_users, ["/bin/sh", "-c", "ls /Users"])
    assert deny.exit_code != 0
