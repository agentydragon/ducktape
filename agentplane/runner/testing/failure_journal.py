"""Native runner evidence preserved for a test whose call phase failed.

The shared `runner` fixture calls `FailureEvidence.preserve_runner` before shutting its runner down, so
every conftest registering that fixture also imports `pytest_runtest_makereport`, which exposes the
call report to fixture teardown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict

from agentplane.runner.service import Runner
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
        logger.warning("no pytest call report for %s; failure evidence unavailable", request.node.nodeid)
        return False
    return bool(report.failed)


@dataclass(frozen=True)
class FailureEvidence:
    """A failed test's evidence directory under Bazel's undeclared outputs, one per test."""

    directory: Path

    @classmethod
    def for_failed_call(cls, request: pytest.FixtureRequest) -> FailureEvidence | None:
        """The test's evidence directory if its call phase failed, else `None`: passing tests keep nothing."""
        if not call_failed(request):
            return None
        return cls(undeclared_outputs_dir() / request.node.name)

    def write_json(self, name: str, data: object) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / name).write_text(json.dumps(data, indent=2, sort_keys=True))

    def copy_native_history(self, runner: Runner) -> None:
        """Copy each session's harness-owned history (`claude/projects`, `codex/sessions`)."""
        for session_id, session in runner.sessions.items():
            for dialect, history in (("claude", "projects"), ("codex", "sessions")):
                source = session.directory / dialect / history
                if source.exists():
                    shutil.copytree(source, self.directory / session_id / dialect / history, dirs_exist_ok=True)

    async def capture_native_journal(self, runner: Runner) -> None:
        """Write a bounded snapshot of each session's native journal."""
        sessions: dict[str, list[dict[str, object]]] = {}
        for session_id, session in runner.sessions.items():
            # Bounded: a wedged journal writer holds the lock this read waits on.
            async with asyncio.timeout(3):
                entries = await session.journal.since(0, limit=512)
            sessions[session_id] = [MessageToDict(entry, preserving_proto_field_name=True) for entry in entries]
        self.write_json("native-journal.json", sessions)

    async def preserve_runner(self, runner: Runner) -> None:
        """Keep the runner's native history files and journal snapshot.

        Each capture degrades on its own, so a broken history copy still leaves the journal.
        """
        try:
            self.copy_native_history(runner)
        except Exception:
            logger.warning("native history capture into %s failed", self.directory, exc_info=True)
        try:
            await self.capture_native_journal(runner)
        except Exception:
            logger.warning("native journal capture into %s failed", self.directory, exc_info=True)
