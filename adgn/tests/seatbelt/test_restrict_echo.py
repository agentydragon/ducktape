import pytest

from adgn.seatbelt.model import DefaultBehavior, FileOp, FileRule, ProcessRule, SBPLPolicy, Subpath
from adgn.seatbelt.runner import run_sandboxed_async
from tests._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC]


@pytest.fixture
def restrictive_echo_policy() -> SBPLPolicy:
    return SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
        ],
    )


async def test_exec_minimal_restrictive_echo(restrictive_echo_policy: SBPLPolicy):
    res = await run_sandboxed_async(restrictive_echo_policy, ["/bin/echo", "HELLO_MINIMAL"])
    assert res.exit_code == 0
    assert res.stdout == b"HELLO_MINIMAL\n"
