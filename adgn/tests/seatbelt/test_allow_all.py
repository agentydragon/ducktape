import pytest

from adgn.seatbelt.model import DefaultBehavior, FileOp, FileRule, ProcessRule, SBPLPolicy, Subpath
from adgn.seatbelt.runner import run_sandboxed_async
from tests._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC]


@pytest.fixture
def allow_all_policy() -> SBPLPolicy:
    return SBPLPolicy(
        default_behavior=DefaultBehavior.ALLOW,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
            FileRule(op=FileOp.FILE_WRITE_STAR, filters=[Subpath(subpath="/")]),
        ],
    )


async def test_exec_allow_all_runs_echo(allow_all_policy: SBPLPolicy):
    res = await run_sandboxed_async(allow_all_policy, ["/bin/sh", "-c", "echo ALLOW_ALL_OK"])
    assert res.exit_code == 0
    assert res.stdout == b"ALLOW_ALL_OK\n"
