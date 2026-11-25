"""A parent Session uses the Action CLI to drive a child in its existing Sandbox."""

from __future__ import annotations

import asyncio
import json
import shlex
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf import json_format
from more_itertools import one

from agentplane.action_service.api.contracts import action_service_pb2
from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.action_cli import CLI, submission
from agentplane.system_tests.driver.agent import Agent
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.policy_objects import PolicyObjects
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
# gazelle:include_dep //agentplane/system_tests:app_fixtures
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin", "agentplane.system_tests.app_fixtures")

_SESSION_ACTIONS = {"create", "submit_input", "observe_input", "get", "read_turns", "cancel_input", "interrupt_turn"}


def _command(*args: str) -> str:
    return shlex.join([CLI, "--json", *args])


def _text_command(*args: str) -> str:
    return shlex.join([CLI, *args])


def _output(request: ModelRequest[Any], call_id: str) -> str:
    output = one(output for output in request.tool_outputs if output.call_id == call_id)
    return "\n".join(output.blocks)


def _json_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def _receipt(output: str) -> action_service_pb2.ActionRequest:
    receipt = action_service_pb2.ActionRequest()
    json_format.Parse(one(line for line in output.splitlines() if line.startswith("{")), receipt)
    return receipt


@dataclass(frozen=True)
class ParentCli:
    """Runs `agentplane` commands through the parent Session; Action receipts must name its Identity.

    `child_answers` maps a child input marker to the scripted reply: the test adds entries, `run` pops them.
    """

    agent: Agent
    model: ScriptedModel[Any]
    child_answers: dict[str, str]
    guidance_marker: str
    caller_identity_name: str
    caller_identity_uid: str

    async def run(self, command: str) -> str:
        """Run one real CLI command through the parent Session, answering child turns by input marker."""
        parent_marker = f"PARENT-CLI-{uuid4()}"
        call_id = f"agentplane-{uuid4().hex[:8]}"
        turn = asyncio.create_task(self.agent.run(f"{parent_marker}: run exactly `{command}` once."))
        issued = False
        try:
            while True:
                request = await self.model.request()
                user_text = "\n".join(request.user_texts)
                child_marker = (
                    next((marker for marker in self.child_answers if marker in user_text), None)
                    if self.guidance_marker in request.system_text
                    else None
                )
                if child_marker is not None:
                    assert "You run inside an Agentplane Sandbox" in request.system_text
                    await self.model.reply(request, Text(self.child_answers.pop(child_marker)))
                    continue

                if not issued:
                    assert parent_marker in user_text
                    await self.model.reply(request, ShellCall(call_id=call_id, command=command))
                    issued = True
                    continue

                output = _output(request, call_id)
                await self.model.reply(request, Text("Done."))
                await turn
                return output
        except BaseException:
            if not turn.done():
                turn.cancel()
                with suppress(asyncio.CancelledError):
                    await turn
            raise

    async def submit_receipt(
        self, group: str, action: str, arguments: dict[str, Any]
    ) -> action_service_pb2.ActionRequest:
        title = f"Parent delegation acceptance: {group}/{action}"
        output = await self.run(_command(*submission(group, action, arguments, title=title, wait=True)))
        receipt = _receipt(output)
        assert receipt.action.group == group
        assert receipt.action.name == action
        assert receipt.HasField("caller_identity")
        assert receipt.caller_identity.name == self.caller_identity_name
        assert receipt.caller_identity.uid == self.caller_identity_uid
        assert not receipt.HasField("caller")
        assert not receipt.HasField("caller_uid")
        return receipt

    async def submit(self, group: str, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        receipt = await self.submit_receipt(group, action, arguments)
        assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
        assert receipt.HasField("execution")
        return json_format.MessageToDict(receipt.execution.result)


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_parent_session_delegates_through_the_action_cli(
    client: Client,
    dispatcher: SessionDispatcherClient,
    cluster: Cluster,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
) -> None:
    offered = await models_offered_for(client, [session_pb2.HARNESS_CODEX])
    parent_view = await sandbox("systest-parent-delegation")
    assert parent_view.kubernetes_diagnostics.HasField("pod")
    await policy_objects.ensure_basic_binding(parent_view)
    caller_identity_name = parent_view.identity_name
    assert caller_identity_name
    identity = json.loads(await cluster.kubectl("get", "identity", caller_identity_name, "-o", "json"))
    caller_identity_uid = identity["metadata"]["uid"]

    parent = await Agent.create(
        client,
        dispatcher,
        sandbox=parent_view.target.name,
        sandbox_uid=parent_view.target.uid,
        harness=session_pb2.HARNESS_CODEX,
        model=offered[session_pb2.HARNESS_CODEX],
    )
    model = scripted.model(parent_view.kubernetes_diagnostics.pod.name, session_pb2.HARNESS_CODEX)
    guidance_marker = f"PARENT-CHILD-GUIDANCE-{uuid4()}"
    child_answers: dict[str, str] = {}
    cli = ParentCli(
        agent=parent,
        model=model,
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
    )

    groups_output = await cli.run(_text_command("actions", "groups"))
    group_keys = {line.split(maxsplit=1)[0] for line in groups_output.splitlines() if line.strip()}
    assert {"environments", "sessions"} <= group_keys

    sessions_output = await cli.run(_text_command("actions", "list", "sessions"))
    session_actions = {
        line.split(maxsplit=1)[0].partition("/")[2]
        for line in sessions_output.splitlines()
        if line.startswith("sessions/")
    }
    assert session_actions == _SESSION_ACTIONS

    show_output = await cli.run(_command("actions", "show", "environments/list"))
    listed_action = one(_json_lines(show_output))
    assert listed_action["name"] == "list"
    assert "limit" in listed_action["inputSchema"]["properties"]

    # Environment discovery is an operator lifecycle API. The parent Identity uses only
    # the Session actions below after the operator provisions its child.
    current = json_format.MessageToDict(
        await client.get_environment(parent_view.target), preserving_proto_field_name=True
    )
    target = current["target"]
    assert identity["metadata"]["name"] == caller_identity_name
    assert identity["metadata"]["uid"] == caller_identity_uid
    assert current["identity_name"] == caller_identity_name
    assert current["observed_state"]["readiness"]["state"] == "READINESS_STATE_READY"
    configuration_group = current["configuration"]
    workspace = configuration_group["workspace"]
    assert workspace["availability"] == "AVAILABILITY_CONFIGURED"
    configuration = one(
        choice for choice in configuration_group["configurations"] if choice["harness"] == "HARNESS_CODEX"
    )
    reasoning_effort = "low"
    assert reasoning_effort in configuration["reasoning_efforts"]

    denied_session_id = str(uuid4())
    denied = await cli.submit_receipt(
        "sessions",
        "create",
        {
            "target": target,
            "session_id": denied_session_id,
            "spec": {
                "harness": configuration["harness"],
                "model": configuration["model"],
                "reasoning_effort": reasoning_effort,
                "cwd": f"{workspace['root'].rstrip('/')}/parent-delegated-{uuid4()}",
            },
            "additional_instructions": guidance_marker,
        },
    )
    assert denied.state == action_service_pb2.ACTION_STATE_FAILED, denied
    assert denied.HasField("execution")
    assert denied.execution.error.kind == action_service_pb2.EXECUTION_ERROR_KIND_BACKEND_REFUSED

    absent = await cli.submit("sessions", "get", {"session_id": denied_session_id})
    assert absent["session"]["kind"] == "not_observed"
    assert absent["history"]["kind"] == "not_observed"

    child = await Agent.create(
        client,
        dispatcher,
        sandbox=parent_view.target.name,
        sandbox_uid=parent_view.target.uid,
        harness=session_pb2.HARNESS_CODEX,
        model=configuration["model"],
        cwd=f"{workspace['root'].rstrip('/')}/parent-delegated-{uuid4()}",
        instructions=guidance_marker,
    )
    session_id = str(child.thread_id)

    existing = await cli.submit("sessions", "get", {"session_id": session_id})
    assert existing["session_id"] == session_id
    assert existing["session"]["kind"] == "observed"
    assert existing["session"]["value"]["identity"] == {"name": caller_identity_name, "uid": caller_identity_uid}

    before_input = await cli.submit("sessions", "read_turns", {"session_id": session_id, "count": 10})
    assert before_input["session_id"] == session_id
    assert before_input["source"] == "history"
    assert not before_input.get("error")
    assert before_input["turns"] == []

    expected_answers: list[str] = []
    expected_inputs: list[str] = []
    for turn_number in (1, 2):
        input_marker = f"CHILD-INPUT-{uuid4()}"
        answer = f"Child retained answer {turn_number}-{uuid4()}"
        child_input = f"{input_marker}: reply with exactly {answer} and stop."
        expected_inputs.append(child_input)
        child_answers[input_marker] = answer
        submitted = await cli.submit(
            "sessions",
            "submit_input",
            {"target": target, "session_id": session_id, "command_id": str(uuid4()), "text": child_input},
        )
        assert submitted["dispatch"]["session_id"] == session_id

        observation = await cli.submit(
            "sessions",
            "observe_input",
            {"session_id": session_id, "command_id": _last_command_id(submitted), "wait_seconds": 30},
        )
        assert observation["observation"]["kind"] == "turn_completed", observation["observation"]
        assert observation["observation"]["turn"]["final_assistant_message"] == answer
        expected_answers.append(answer)

        snapshot = await cli.submit("sessions", "get", {"session_id": session_id})
        assert snapshot["session_id"] == session_id
        assert snapshot["session"]["kind"] == "observed"
        assert snapshot["history"]["kind"] == "observed"

        page = await cli.submit("sessions", "read_turns", {"session_id": session_id, "count": 10})
        assert page["session_id"] == session_id
        assert page["source"] == "history"
        assert not page.get("error")
        turns = page["turns"]
        assert [turn["status"] for turn in turns] == ["TURN_STATUS_COMPLETED"] * turn_number
        assert [turn["final_assistant_message"] for turn in turns] == expected_answers
        assert [entry["text"] for turn in turns for entry in turn["input"]] == expected_inputs

    assert not child_answers


def _last_command_id(submitted: dict[str, Any]) -> str:
    """Read the exact Dispatch command ID returned by the CLI receipt."""
    command_id = submitted["dispatch"]["command_id"]
    assert isinstance(command_id, str)
    return command_id


if __name__ == "__main__":
    pytest_bazel.main()
