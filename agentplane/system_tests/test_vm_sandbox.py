"""A Sandbox on the VM template runs a turn on each harness, with the scripted model."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.system_tests.agent import Agent
from agentplane.system_tests.conftest import VM_TEMPLATE, VM_WORKING_DIRECTORY, Sandboxes
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import Text

PING = "Say exactly the word PING and nothing else. Do not use any tool."


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
async def test_a_vm_sandbox_runs_a_turn(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, offered_model: str, scripted: ScriptedUpstream
) -> None:
    view = await sandbox(f"systest-vm-{harness}", template=VM_TEMPLATE)
    assert view.pod is not None
    model = scripted.model(view.pod.name, harness)
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=offered_model, cwd=VM_WORKING_DIRECTORY)
    async with asyncio.TaskGroup() as turns:
        turn = turns.create_task(agent.run(PING))
        await model.reply(await model.request(), Text("PING"))
    # The scripted reply is the thread's answer: the guest's model traffic came back through the launcher.
    assert "PING" in turn.result().answer, turn.result().transcript


if __name__ == "__main__":
    pytest_bazel.main()
