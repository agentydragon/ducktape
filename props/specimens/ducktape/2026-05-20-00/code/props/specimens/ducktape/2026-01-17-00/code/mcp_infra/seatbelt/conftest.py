from __future__ import annotations

import asyncio

import pytest

from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.model import Action, DefaultBehavior, FileOp, FileRule, ProcessRule, SBPLPolicy, Subpath
from mcp_infra.seatbelt.runner import apopen, run_sandboxed_async

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


@pytest.fixture
async def cat_process(require_sandbox_exec, allow_all_policy: SBPLPolicy):
    p = await apopen(["/bin/sh", "-c", "cat"], allow_all_policy, trace=True)
    try:
        yield p
    finally:
        if p.stdin:
            p.stdin.close()
        try:
            await asyncio.wait_for(p.wait(), timeout=2)
        except Exception:
            p.kill()
            await p.wait()
        p.cleanup()


@pytest.fixture
def run_async(require_sandbox_exec):
    async def _run(policy: SBPLPolicy, argv: list[str], *, trace: bool = False):
        rr = await run_sandboxed_async(
            policy, argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, trace=trace
        )
        if rr.exit_code != 0:
            print("\n=== seatbelt diagnostics (async) ===")
            print(f"cmd: {' '.join(rr.cmd)}")
            print("-- policy.sb (head) --\n" + "\n".join(rr.policy_text.splitlines()[:25]))
            if rr.unified_sandbox_denies_text:
                tail = "\n".join((rr.unified_sandbox_denies_text or "").splitlines()[-120:])
                print("-- unified sandbox denies (tail) --\n" + tail)
            if rr.trace_text:
                print("-- seatbelt trace (tail) --\n" + "\n".join((rr.trace_text or "").splitlines()[-120:]))
        return rr

    return _run


# (No anyio_backend override — seatbelt tests use pytest-asyncio directly.)
