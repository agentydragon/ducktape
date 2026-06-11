from __future__ import annotations

import pytest

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.model import Action, DefaultBehavior, FileOp, FileRule, ProcessRule, SBPLPolicy, Subpath

pytestmark = [*REQUIRES_SANDBOX_EXEC]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/seatbelt" not in str(item.fspath):
            continue
        for mark in REQUIRES_SANDBOX_EXEC:
            if item.get_closest_marker(mark.name) is None:
                item.add_marker(mark)


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


@pytest.fixture
def restrictive_echo_policy() -> SBPLPolicy:
    # Default-deny + minimal read needed to exec echo on this host
    return SBPLPolicy(
        default_behavior=DefaultBehavior.DENY,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
        ],
    )


@pytest.fixture
def policy_deny_users(allow_all_policy: SBPLPolicy) -> SBPLPolicy:
    p: SBPLPolicy = allow_all_policy.model_copy(deep=True)
    p.files += [
        FileRule(op=FileOp.FILE_READ_STAR, action=Action.DENY, filters=[Subpath(subpath="/Users")]),
        FileRule(op=FileOp.FILE_READ_METADATA, action=Action.DENY, filters=[Subpath(subpath="/Users")]),
    ]
    return p


# (No anyio_backend override — seatbelt tests use pytest-asyncio directly.)
