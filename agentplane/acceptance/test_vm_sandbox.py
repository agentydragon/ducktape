"""A Sandbox on the `runner-vm` template runs a turn, checked against a deployed Agentplane.

The guest boots, formats its workspace disk and dials the gateway on its own schedule, so the
Sandbox's `Ready` condition (its virt-launcher Pod is Ready) comes before the runner answers;
`Agent.open` retries until the runner does.
"""

from __future__ import annotations

import pytest_bazel

from agentplane.acceptance.agent import Agent
from agentplane.acceptance.conftest import Sandboxes
from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2

# gazelle:include_dep @pypi//protobuf

VM_TEMPLATE = "runner-vm"
PING = "Say exactly the word PING and nothing else. Do not use any tool."


async def test_a_vm_sandbox_runs_a_turn(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, model: str
) -> None:
    view = await sandbox(f"accept-vm-{harness}", template=VM_TEMPLATE)
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
    turn = await agent.run(PING)
    assert "PING" in turn.answer, turn.transcript


if __name__ == "__main__":
    pytest_bazel.main()
