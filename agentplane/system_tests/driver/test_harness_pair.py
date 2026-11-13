"""Harness order, model routing and failure identification before sharing live suite fixtures."""

import asyncio
from unittest.mock import AsyncMock, create_autospec

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.system_tests.driver import harness_pair

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


async def test_claude_finishes_before_codex_starts_each_with_its_own_model(client: Client) -> None:
    events: list[tuple[str, session_pb2.Harness, str]] = []

    async def run(harness: session_pb2.Harness, model: str) -> None:
        events.append(("start", harness, model))
        # Yield to the loop: a concurrent peer would start here.
        await asyncio.sleep(0)
        events.append(("end", harness, model))

    await harness_pair.run_harness_pair(client, run)
    assert events == [
        ("start", session_pb2.HARNESS_CLAUDE, "claude-model"),
        ("end", session_pb2.HARNESS_CLAUDE, "claude-model"),
        ("start", session_pb2.HARNESS_CODEX, "codex-model"),
        ("end", session_pb2.HARNESS_CODEX, "codex-model"),
    ]


async def test_a_claude_failure_names_its_harness_and_codex_does_not_run(client: Client) -> None:
    ran: list[session_pb2.Harness] = []

    async def run(harness: session_pb2.Harness, model: str) -> None:
        ran.append(harness)
        raise ValueError("scenario failed")

    with pytest.raises(ValueError, match="scenario failed") as failure:
        await harness_pair.run_harness_pair(client, run)
    assert ran == [session_pb2.HARNESS_CLAUDE]
    assert failure.value.__notes__ == ["harness=HARNESS_CLAUDE"]


async def test_reserved_runners_overlap_and_keep_their_own_models(client: Client) -> None:
    started: dict[session_pb2.Harness, str] = {}
    both_started = asyncio.Event()
    finished: set[session_pb2.Harness] = set()

    async def run(harness: session_pb2.Harness, model: str) -> None:
        started[harness] = model
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        finished.add(harness)

    async with asyncio.timeout(2):
        await harness_pair.run_harness_pair_concurrently(client, run)
    assert started == {session_pb2.HARNESS_CLAUDE: "claude-model", session_pb2.HARNESS_CODEX: "codex-model"}
    assert finished == set(started)


async def test_failed_harness_waits_for_its_peer_to_clean_up(client: Client) -> None:
    peer_started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def run(harness: session_pb2.Harness, model: str) -> None:
        if harness == session_pb2.HARNESS_CLAUDE:
            await peer_started.wait()
            raise ValueError("scenario failed")
        try:
            peer_started.set()
            await asyncio.Event().wait()
        finally:
            # Cleanup itself can yield, as a real Session close does.
            await asyncio.sleep(0)
            cleaned_up.set()

    async with asyncio.timeout(2):
        with pytest.raises(ExceptionGroup) as failure:
            await harness_pair.run_harness_pair_concurrently(client, run)
    assert cleaned_up.is_set()
    assert len(failure.value.exceptions) == 1
    error = failure.value.exceptions[0]
    assert isinstance(error, ValueError)
    assert error.__notes__ == ["harness=HARNESS_CLAUDE"]


if __name__ == "__main__":
    pytest_bazel.main()
