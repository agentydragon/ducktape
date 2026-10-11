"""Sandbox teardown seals every session: it can still be read to its end, never resumed or extended."""

from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.client import Attachment, RunnerClient
from agentplane.runner.config import RunnerConfig
from agentplane.runner.errors import RunnerError, SessionSealedError
from agentplane.runner.service import Runner, serve
from agentplane.runner.store import SessionRecord

# gazelle:include_dep @pypi//protobuf

SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/test-workspace", model="test-model")
SESSIONS = ("test-session-a", "test-session-b")


@pytest.fixture
def config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(state_dir=tmp_path / "state", harness_environment={})


@pytest.fixture
async def last_cursors(config: RunnerConfig) -> dict[str, int]:
    """Sessions whose harness already exited, written by a runner that has since stopped."""
    runner = Runner(config)
    try:
        cursors = {}
        for session_id in SESSIONS:
            runner.store.write(session_id, SessionRecord.from_spec(SPEC))
            journal = (await runner._load(session_id)).journal
            await journal.append(event_pb2.HarnessStarted(pid=1))
            cursors[session_id] = (await journal.append(event_pb2.HarnessExited(exit_code=0))).cursor
        return cursors
    finally:
        await runner.stop()


@pytest.fixture
async def client(config: RunnerConfig, last_cursors: dict[str, int]) -> AsyncIterator[RunnerClient]:
    del last_cursors
    server, runner, port = await serve(config)
    client = RunnerClient(grpc.aio.insecure_channel(f"127.0.0.1:{port}"), capture_history=True)
    try:
        yield client
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)


async def read_to_seal(attachment: Attachment) -> tuple[list[int], int]:
    with pytest.raises(SessionSealedError) as sealed:
        await attachment.until(lambda _: False)
    return [entry.cursor for entry in attachment.seen], sealed.value.cursor


async def test_seal_ends_every_session_and_refuses_resume(
    client: RunnerClient, config: RunnerConfig, last_cursors: dict[str, int]
) -> None:
    seals = {seal.session_id: seal.cursor for seal in (await client.seal()).sessions}
    assert seals == last_cursors
    assert {seal.session_id: seal.cursor for seal in (await client.seal()).sessions} == seals
    async with await client.attach("test-session-a", after_cursor=1) as attachment:
        assert await read_to_seal(attachment) == (list(range(2, seals["test-session-a"] + 1)), seals["test-session-a"])
    for session_id in ("test-session-a", "test-session-new"):
        with pytest.raises(RunnerError):
            await client.attach(session_id, spec=SPEC)
    assert not (config.state_dir / "sessions" / "test-session-new").exists()


async def test_a_restarted_runner_keeps_the_seal(config: RunnerConfig, last_cursors: dict[str, int]) -> None:
    for _ in range(2):
        server, runner, port = await serve(config)
        client = RunnerClient(grpc.aio.insecure_channel(f"127.0.0.1:{port}"), capture_history=True)
        try:
            assert {seal.session_id: seal.cursor for seal in (await client.seal()).sessions} == last_cursors
            async with await client.attach("test-session-b") as attachment:
                cursors, sealed = await read_to_seal(attachment)
            assert sealed == cursors[-1] == last_cursors["test-session-b"]
            with pytest.raises(RunnerError):
                await client.attach("test-session-b", spec=SPEC)
        finally:
            await client.close()
            await runner.stop()
            await server.stop(0)


if __name__ == "__main__":
    pytest_bazel.main()
