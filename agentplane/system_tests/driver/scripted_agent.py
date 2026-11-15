"""Drive one Agent session with explicit `agentplane` CLI commands."""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from google.protobuf import json_format
from more_itertools import one

from agentplane.action_service.api.contracts import action_service_pb2
from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import Agent
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

# Where the environment disk mounts the CLI in a Sandbox Pod (`agentplane/env/images/BUILD.bazel`).
CLI = "/run/agentplane/environment/bin/agentplane"


def cli_command(*args: str) -> str:
    return shlex.join([CLI, "--json", *args])


def submission(group: str, action: str, arguments: dict[str, Any], *, title: str, wait: bool) -> list[str]:
    """Build CLI arguments for one Action request under a fresh idempotency key."""
    return [
        "actions",
        "submit",
        f"{group}/{action}",
        "--title",
        title,
        "--arguments",
        json.dumps(arguments),
        "--key",
        str(uuid4()),
        *(["--wait"] if wait else []),
    ]


def _action_receipt(request: ModelRequest[Any], call_id: str) -> action_service_pb2.ActionRequest:
    reported = one(output for output in request.tool_outputs if output.call_id == call_id)
    # The harness frames the command output with its own lines; `--json` prints the receipt as one.
    receipt = action_service_pb2.ActionRequest()
    json_format.Parse(
        one(line for block in reported.blocks for line in block.splitlines() if line.startswith("{")), receipt
    )
    return receipt


async def play_cli_turn(model: ScriptedModel[Any], *args: str) -> action_service_pb2.ActionRequest:
    """Return the Action receipt the harness reported after running one CLI command."""
    with tracer.start_as_current_span("system_test.mcp.turn"):
        call_id = f"agentplane-{uuid4().hex[:8]}"
        request = await model.request()
        await model.reply(request, ShellCall(call_id=call_id, command=cli_command(*args)))
        request = await model.request()
        await model.reply(request, Text("Done."))
        return _action_receipt(request, call_id)


@dataclass(frozen=True)
class ScriptedAgent:
    """An Agent session whose model the scenario scripts one CLI command at a time."""

    agent: Agent
    model: ScriptedModel[Any]

    @classmethod
    async def create(
        cls,
        client: Client,
        dispatcher: SessionDispatcherClient,
        sandbox: environment_service_pb2.EnvironmentDescriptor,
        *,
        harness: session_pb2.Harness,
        model: str,
        upstream: ScriptedUpstream,
    ) -> ScriptedAgent:
        assert sandbox.kubernetes_diagnostics.HasField("pod")
        agent = await Agent.create(
            client,
            dispatcher,
            sandbox=sandbox.target.name,
            sandbox_uid=sandbox.target.uid,
            harness=harness,
            model=model,
        )
        return cls(agent=agent, model=upstream.model(sandbox.kubernetes_diagnostics.pod.name, harness))

    async def run(self, *args: str) -> action_service_pb2.ActionRequest:
        """Run `agentplane --json ARGS` in the session and return its receipt."""
        async with asyncio.TaskGroup() as turns:
            turns.create_task(self.agent.run(f"Run {cli_command(*args)}"))
            # The TaskGroup waits for the turn to finish before this returns.
            return await play_cli_turn(self.model, *args)

    async def submit(
        self, group: str, action: str, arguments: dict[str, Any], *, title: str, wait: bool
    ) -> action_service_pb2.ActionRequest:
        return await self.run(*submission(group, action, arguments, title=title, wait=wait))

    async def read_action_policy(self) -> action_service_pb2.CallerActionPolicy:
        """Read the Action policy resolved for this session's Identity."""
        with tracer.start_as_current_span("system_test.mcp.identity_action_policy"):
            call_id = f"agentplane-{uuid4().hex[:8]}"
            async with asyncio.TaskGroup() as turns:
                turns.create_task(self.agent.run("Show the Action policy for this Identity."))
                request = await self.model.request()
                await self.model.reply(request, ShellCall(call_id=call_id, command=cli_command("actions", "policy")))
                response = await self.model.request()
                output = one(output for output in response.tool_outputs if output.call_id == call_id)
                line = one(line for block in output.blocks for line in block.splitlines() if line.startswith("{"))
                policy = action_service_pb2.CallerActionPolicy()
                json_format.Parse(line, policy)
                await self.model.reply(response, Text("Done."))
            return policy
