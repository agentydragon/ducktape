"""Harness order, model routing and failure identification before sharing live suite fixtures."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, create_autospec

import pytest

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.system_tests.driver import harness_pair
from agentplane.system_tests.driver.cleanup import CleanupError
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes

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


async def test_nested_sandbox_cleanup_failure_preserves_body_error_and_stops_the_batch(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def scope(*args: object) -> AsyncIterator[Sandboxes]:
        try:
            yield AsyncMock()
        finally:
            raise RuntimeError("guest deletion failed")

    async def run(harness: session_pb2.Harness, model: str, sandboxes: Sandboxes) -> None:
        assert harness == session_pb2.HARNESS_CLAUDE
        raise ValueError("scenario failed")

    monkeypatch.setattr(harness_pair, "sandbox_scope", scope)
    cluster: Cluster = create_autospec(Cluster, instance=True)
    with pytest.raises(CleanupError) as failed:
        await harness_pair.run_harness_pair_in_own_sandboxes(cluster, client, run)
    cause = failed.value.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert cause.subgroup(ValueError) is not None
    assert cause.subgroup(RuntimeError) is not None
