"""Shared failure-report and native runner-journal capture for Agentplane tests."""

from __future__ import annotations

import json
from collections.abc import Generator

import pytest
from google.protobuf.json_format import MessageToDict

from agentplane.runner.testing.fixtures import RunnerHandle
from util.testing.undeclared_outputs import undeclared_outputs_dir

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
    return report is not None and report.failed


async def capture_native_journal(request: pytest.FixtureRequest, runner: RunnerHandle) -> None:
    """Write the runner's bounded native journal snapshot to this test's undeclared outputs."""
    sessions: dict[str, list[dict[str, object]]] = {}
    for session_id, session in runner.runner.sessions.items():
        entries = await session.journal.since(0, limit=512)
        sessions[session_id] = [MessageToDict(entry, preserving_proto_field_name=True) for entry in entries]
    (undeclared_outputs_dir() / f"{request.node.name}-native-journal.json").write_text(
        json.dumps(sessions, indent=2, sort_keys=True)
    )
