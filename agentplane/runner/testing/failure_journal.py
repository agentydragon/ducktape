"""Native runner evidence preserved for a test whose call phase failed.

A conftest using `call_failed` or `failed_native_journal` also imports `pytest_runtest_makereport`,
which exposes the call report to the fixture's teardown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator, Generator

import pytest
from google.protobuf.json_format import MessageToDict

from agentplane.runner.testing.fixtures import RunnerHandle
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

CALL_REPORT = pytest.StashKey[pytest.TestReport]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Expose the call report to teardown fixtures without changing test outcomes."""
    report = yield
    if call.when == "call":
        item.stash[CALL_REPORT] = report
    return report


def call_failed(request: pytest.FixtureRequest) -> bool:
    """Whether the test's call phase failed, as opposed to setup or teardown."""
    report = request.node.stash.get(CALL_REPORT, None)
    if report is None:
        logger.warning("no pytest call report for %s; failure journal unavailable", request.node.nodeid)
        return False
    return bool(report.failed)


async def capture_native_journal(request: pytest.FixtureRequest, runner: RunnerHandle) -> None:
    """Write the runner's bounded native journal snapshot to this test's undeclared outputs."""
    sessions: dict[str, list[dict[str, object]]] = {}
    for session_id, session in runner.runner.sessions.items():
        # Bounded: a wedged journal writer holds the lock this read waits on.
        async with asyncio.timeout(3):
            entries = await session.journal.since(0, limit=512)
        sessions[session_id] = [MessageToDict(entry, preserving_proto_field_name=True) for entry in entries]
    (undeclared_outputs_dir() / f"{request.node.name}-native-journal.json").write_text(
        json.dumps(sessions, indent=2, sort_keys=True)
    )


@pytest.fixture
async def failed_native_journal(
    request: pytest.FixtureRequest, runner: RunnerHandle, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[None]:
    """On a failed call phase, write each session's native history files and journal snapshot."""
    caplog.set_level(logging.INFO, logger="httpx")
    yield
    if not call_failed(request):
        return
    for session_id, session in runner.runner.sessions.items():
        for dialect, history in (("claude", "projects"), ("codex", "sessions")):
            source = session.directory / dialect / history
            if source.exists():
                shutil.copytree(
                    source,
                    undeclared_outputs_dir() / request.node.name / session_id / dialect / history,
                    dirs_exist_ok=True,
                )
    await capture_native_journal(request, runner)
