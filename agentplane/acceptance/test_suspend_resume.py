"""Completed conversations survive a real Sandbox suspend/resume on both harnesses."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.acceptance.agent import Agent
from agentplane.app.client import Client, has_ready_pod
from agentplane.app.sandbox_models import SandboxView
from agentplane.runner import protocol_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.models import OperatingMode

Sandboxes = Callable[..., Awaitable[SandboxView]]
POD_TRANSITION_SECONDS = 300.0


async def _wait_until_suspended_without_pod(client: Client, name: str) -> None:
    """Wait for the old Pod to disappear before the same Sandbox is resumed."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(POD_TRANSITION_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.sandbox(name)
            assert view.operating_mode is OperatingMode.SUSPENDED, f"{name} is still {view.operating_mode}"
            assert view.pod is None, f"{name} still has its old Pod: {view.pod}"


async def test_two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
    client: Client, sandbox: Sandboxes
) -> None:
    view = await sandbox("accept-suspend-resume")
    catalog = await client.models()
    markers = {protocol_pb2.HARNESS_CLAUDE: f"CLAUDE-{uuid4().hex}", protocol_pb2.HARNESS_CODEX: f"CODEX-{uuid4().hex}"}
    agents: dict[protocol_pb2.Harness, Agent] = {}

    for harness in (protocol_pb2.HARNESS_CLAUDE, protocol_pb2.HARNESS_CODEX):
        harness_name = protocol_pb2.Harness.Name(harness)
        offered = catalog.harnesses[Harness(harness_name)]
        assert offered, f"the deployment offers no model for {harness_name}"
        agent = await Agent.open(client, sandbox=view.name, harness=harness, model=offered[0])
        seed = await agent.run(
            f"Remember this exact token for my next message: {markers[harness]}. Do not use tools. Reply with only ACK."
        )
        assert seed.input_confirmed, f"{harness_name} did not confirm the seed input"
        agents[harness] = agent

    original_threads = {harness: agent.thread_id for harness, agent in agents.items()}
    assert len(set(original_threads.values())) == 2
    await client.suspend_sandbox(view.name)
    await _wait_until_suspended_without_pod(client, view.name)
    await client.resume_sandbox(view.name)

    async for attempt in AsyncRetrying(stop=stop_after_delay(POD_TRANSITION_SECONDS), wait=wait_fixed(2), reraise=True):
        with attempt:
            resumed = await client.sandbox(view.name)
            if not has_ready_pod(resumed):
                raise AssertionError(f"{view.name} has no ready, authorized Pod: {resumed.pod}")

    for harness, agent in agents.items():
        await agent.resume()
        turn = await agent.run("Do not use tools. What exact token did I ask you to remember? Reply with only it.")
        expected = markers[harness]
        other_harness = (
            protocol_pb2.HARNESS_CODEX if harness == protocol_pb2.HARNESS_CLAUDE else protocol_pb2.HARNESS_CLAUDE
        )
        other = markers[other_harness]
        assert turn.resumed, f"{protocol_pb2.Harness.Name(harness)} did not report native session resume"
        assert turn.input_confirmed, f"{protocol_pb2.Harness.Name(harness)} did not confirm the resumed input"
        assert expected in turn.answer, f"{protocol_pb2.Harness.Name(harness)} forgot {expected}:\n{turn.transcript}"
        assert other not in turn.answer, f"{protocol_pb2.Harness.Name(harness)} received the other Thread's token"


if __name__ == "__main__":
    pytest_bazel.main()
