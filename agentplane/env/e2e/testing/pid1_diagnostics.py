"""Preserve PID1 test waits before Bazel kills the whole shard, including fixture setup."""

import asyncio
import faulthandler
import json
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


@dataclass(frozen=True)
class WaitEvidence:
    directory: Path

    def record(self, event: str, **fields: object) -> None:
        with (self.directory / "progress.jsonl").open("a") as output:
            output.write(json.dumps({"at": datetime.now(UTC).isoformat(), "event": event, **fields}) + "\n")

    def waiting(self, condition: str, producer: str, **fields: object) -> None:
        self.record("waiting", condition=condition, producer=producer, **fields)


_ACTIVE = pytest.StashKey[WaitEvidence]()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, bool, bool]:
    # A function fixture starts too late to diagnose a stuck session-scoped kubeconfig fixture.
    directory = undeclared_outputs_dir() / "pid1-diagnostics" / item.name
    directory.mkdir(parents=True, exist_ok=True)
    evidence = WaitEvidence(directory)
    item.session.stash[_ACTIVE] = evidence
    with (directory / "python-threads.txt").open("w") as output:
        evidence.record("protocol-start", nodeid=item.nodeid)
        # stdlib's native watchdog still dumps if Docker blocks the event-loop thread.
        # This observes the existing deadline; it never cancels or retries test work.
        faulthandler.dump_traceback_later(10, repeat=True, file=output)
        try:
            return (yield)
        finally:
            faulthandler.cancel_dump_traceback_later()
            evidence.record("protocol-end")
            del item.session.stash[_ACTIVE]


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
) -> Generator[None, Any, Any]:
    evidence = request.session.stash[_ACTIVE]
    evidence.record("fixture-start", fixture=fixturedef.argname)
    result = yield
    evidence.record("fixture-ready", fixture=fixturedef.argname)
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    evidence = item.session.stash[_ACTIVE]
    evidence.record("teardown-start")
    yield
    evidence.record("teardown-end")


@pytest.fixture
def pid1_evidence(request: pytest.FixtureRequest) -> WaitEvidence:
    return request.session.stash[_ACTIVE]


@pytest.fixture(autouse=True)
async def pid1_task_stacks(pid1_evidence: WaitEvidence) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    timer: asyncio.TimerHandle
    with (pid1_evidence.directory / "asyncio-tasks.txt").open("w") as output:

        def dump() -> None:
            nonlocal timer
            output.write(f"{datetime.now(UTC).isoformat()}\n")
            for task in asyncio.all_tasks(loop):
                task.print_stack(file=output)
            output.flush()
            timer = loop.call_later(10, dump)

        timer = loop.call_later(10, dump)
        try:
            yield
        finally:
            timer.cancel()
