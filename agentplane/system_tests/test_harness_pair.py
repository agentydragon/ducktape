"""Check actual overlap and cancellation before sharing live suite fixtures."""

import asyncio
from unittest.mock import AsyncMock, create_autospec

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.system_tests import harness_pair

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Client:
    monkeypatch.setattr(
        harness_pair,
        "models_offered_for",
        AsyncMock(return_value={session_pb2.HARNESS_CLAUDE: "claude-model", session_pb2.HARNESS_CODEX: "codex-model"}),
    )
    mocked: Client = create_autospec(Client, instance=True)
    return mocked


async def test_both_harnesses_overlap_and_receive_their_own_models(client: Client) -> None:
    peers = asyncio.Barrier(2)
    seen: dict[session_pb2.Harness, str] = {}

    async def run(harness: session_pb2.Harness, model: str) -> None:
        seen[harness] = model
        await peers.wait()

    async with asyncio.timeout(2):
        await harness_pair.run_harness_pair(client, run)
    assert seen == {session_pb2.HARNESS_CLAUDE: "claude-model", session_pb2.HARNESS_CODEX: "codex-model"}


async def test_failure_identifies_the_harness_and_finishes_peer_cleanup(client: Client) -> None:
    peer_started = asyncio.Event()
    cleaned = False

    async def run(harness: session_pb2.Harness, model: str) -> None:
        nonlocal cleaned
        if harness == session_pb2.HARNESS_CLAUDE:
            await peer_started.wait()
            raise ValueError("scenario failed")
        try:
            peer_started.set()
            await asyncio.Event().wait()
        finally:
            cleaned = True

    async with asyncio.timeout(2):
        with pytest.raises(ExceptionGroup) as failure:
            await harness_pair.run_harness_pair(client, run)
    assert cleaned
    assert len(failure.value.exceptions) == 1
    error = failure.value.exceptions[0]
    assert isinstance(error, ValueError)
    assert str(error) == "scenario failed"
    assert error.__notes__ == ["harness=HARNESS_CLAUDE"]


if __name__ == "__main__":
    pytest_bazel.main()
