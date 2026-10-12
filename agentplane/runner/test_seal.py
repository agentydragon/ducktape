"""Sandbox teardown seals every session: it can still be read to its end, never resumed or extended."""

from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.runner import Runner
from agentplane.runner.store import SessionRecord
from agentplane.runner.testing.driver import RunnerDriver, SessionFollow, SessionSealedError, refusal, running
from agentplane.runner.testing.gateway import Gateway

# gazelle:include_dep @pypi//protobuf

SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/test-workspace", model="test-model")
SESSIONS = ("test-session-a", "test-session-b")


@pytest.fixture
def config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(state_dir=tmp_path / "state", harness_environment={})


@pytest.fixture
async def last_cursors(config: RunnerConfig) -> dict[str, int]:
    """Sessions whose harness already exited, written by a runner that has since stopped."""
    async with Runner(config) as runner:
        cursors = {}
        for session_id in SESSIONS:
            runner.store.write(session_id, SessionRecord.from_spec(SPEC))
            journal = (await runner._load(session_id)).journal
            await journal.append(event_pb2.HarnessStarted(pid=1))
            cursors[session_id] = (await journal.append(event_pb2.HarnessExited(exit_code=0))).cursor
        return cursors


@pytest.fixture
async def driver(config: RunnerConfig, last_cursors: dict[str, int], gateway: Gateway) -> AsyncIterator[RunnerDriver]:
    del last_cursors
    async with running(config, gateway):
        yield RunnerDriver(gateway)


async def read_to_seal(session: SessionFollow) -> tuple[list[int], int]:
    with pytest.raises(SessionSealedError) as sealed:
        await session.until(lambda _: False)
    return [entry.cursor for entry in session.seen], sealed.value.cursor


async def test_seal_ends_every_session_and_refuses_resume(
    driver: RunnerDriver, config: RunnerConfig, last_cursors: dict[str, int]
) -> None:
    seals = {seal.session_id: seal.cursor for seal in (await driver.seal()).sessions}
    assert seals == last_cursors
    assert {seal.session_id: seal.cursor for seal in (await driver.seal()).sessions} == seals
    session = await driver.attach("test-session-a", after_cursor=1)
    assert await read_to_seal(session) == (list(range(2, seals["test-session-a"] + 1)), seals["test-session-a"])
    for session_id in ("test-session-a", "test-session-new"):
        assert await refusal(driver.attach(session_id, spec=SPEC)) == grpc.StatusCode.FAILED_PRECONDITION
    assert not (config.state_dir / "sessions" / "test-session-new").exists()


async def test_a_restarted_runner_keeps_the_seal(
    config: RunnerConfig, last_cursors: dict[str, int], gateway: Gateway
) -> None:
    driver = RunnerDriver(gateway)
    for _ in range(2):
        async with running(config, gateway):
            assert {seal.session_id: seal.cursor for seal in (await driver.seal()).sessions} == last_cursors
            cursors, sealed = await read_to_seal(await driver.attach("test-session-b"))
            assert sealed == cursors[-1] == last_cursors["test-session-b"]
            assert await refusal(driver.attach("test-session-b", spec=SPEC)) == grpc.StatusCode.FAILED_PRECONDITION


if __name__ == "__main__":
    pytest_bazel.main()
