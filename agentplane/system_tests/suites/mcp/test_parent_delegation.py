"""A parent Session uses the Action CLI to drive a child in its existing Sandbox."""

from __future__ import annotations

import asyncio
import json
import shlex
from contextlib import suppress
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf import json_format
from more_itertools import one

from agentplane.action_service import action_service_pb2
from agentplane.app.testing.client import Client
from agentplane.app.testing.models import SandboxView
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.agent import Agent
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.offered_models import models_offered_for
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scenarios import mcp
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

CLI = "/run/agentplane/environment/bin/agentplane"
_SESSION_ACTIONS = {"open", "submit_input", "observe_input", "get", "read_turns", "cancel_input", "interrupt_turn"}


def _command(*args: str) -> str:
    return shlex.join([CLI, "--json", *args])


def _action_command(group: str, action: str, arguments: dict[str, Any]) -> list[str]:
    return [
        "actions",
        "submit",
        f"{group}/{action}",
        "--title",
        f"Parent delegation acceptance: {group}/{action}",
        "--arguments",
        json.dumps(arguments),
        "--key",
        str(uuid4()),
        "--wait",
    ]


def _output(request: ModelRequest[Any], call_id: str) -> str:
    output = one(output for output in request.tool_outputs if output.call_id == call_id)
    return "\n".join(output.blocks)


def _json_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def _receipt(output: str) -> action_service_pb2.ActionRequest:
    receipt = action_service_pb2.ActionRequest()
    json_format.Parse(one(line for line in output.splitlines() if line.startswith("{")), receipt)
    return receipt


async def _run_parent_cli(
    agent: Agent, model: ScriptedModel[Any], command: str, *, child_answers: dict[str, str], guidance_marker: str
) -> str:
    """Run one real CLI command through the parent Session, answering child turns by input marker."""
    parent_marker = f"PARENT-CLI-{uuid4()}"
    call_id = f"agentplane-{uuid4().hex[:8]}"
    turn = asyncio.create_task(agent.run(f"{parent_marker}: run exactly `{command}` once."))
    issued = False
    try:
        while True:
            request = await model.request()
            user_text = "\n".join(request.user_texts)
            child_marker = (
                next((marker for marker in child_answers if marker in user_text), None)
                if guidance_marker in request.system_text
                else None
            )
            if child_marker is not None:
                assert "You run inside an Agentplane Sandbox" in request.system_text
                await model.reply(request, Text(child_answers.pop(child_marker)))
                continue

            if not issued:
                assert parent_marker in user_text
                await model.reply(request, ShellCall(call_id=call_id, command=command))
                issued = True
                continue

            output = _output(request, call_id)
            await model.reply(request, Text("Done."))
            await turn
            return output
    except BaseException:
        if not turn.done():
            turn.cancel()
            with suppress(asyncio.CancelledError):
                await turn
        raise


async def _submit(
    agent: Agent,
    model: ScriptedModel[Any],
    group: str,
    action: str,
    arguments: dict[str, Any],
    *,
    child_answers: dict[str, str],
    guidance_marker: str,
    caller_namespace: str,
    caller_name: str,
    caller_uid: str,
) -> dict[str, Any]:
    output = await _run_parent_cli(
        agent,
        model,
        _command(*_action_command(group, action, arguments)),
        child_answers=child_answers,
        guidance_marker=guidance_marker,
    )
    receipt = _receipt(output)
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.action.group == group
    assert receipt.action.name == action
    assert receipt.caller.namespace == caller_namespace
    assert receipt.caller.name == caller_name
    assert receipt.HasField("caller_uid")
    assert receipt.caller_uid == caller_uid
    assert receipt.HasField("execution")
    return json_format.MessageToDict(receipt.execution.result)


async def _wait_basic_policy(cluster: Cluster, view: SandboxView, policy_objects: mcp.PolicyObjects) -> None:
    inventory = json.loads(await cluster.kubectl("get", POLICY_BINDINGS_PLURAL, "-o", "json"))
    subject = view.service_account
    bindings = [
        binding
        for binding in inventory["items"]
        if binding.get("spec", {}).get("subject", {}).get("namespace") == subject.namespace
        and binding.get("spec", {}).get("subject", {}).get("name") == subject.name
        and "basic" in binding.get("spec", {}).get("policySets", [])
    ]
    if not bindings:
        name = await policy_objects.create_binding(f"{view.name}-delegation-basic", sandbox=view, policy_sets=["basic"])
        await policy_objects.wait_actions_ready(name)
        return
    for binding in bindings:
        await policy_objects.wait_actions_ready(binding["metadata"]["name"])


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_parent_session_delegates_through_the_action_cli(
    client: Client,
    dispatcher: SessionDispatcherClient,
    cluster: Cluster,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    offered = await models_offered_for(client, [session_pb2.HARNESS_CODEX])
    parent_view = await sandbox("systest-parent-delegation")
    assert parent_view.pod is not None
    await _wait_basic_policy(cluster, parent_view, policy_objects)
    service_account = json.loads(
        await cluster.kubectl("get", "serviceaccount", parent_view.service_account.name, "-o", "json")
    )
    caller_uid = service_account["metadata"]["uid"]
    caller_namespace = parent_view.service_account.namespace
    caller_name = parent_view.service_account.name

    parent = await Agent.open(
        client,
        dispatcher,
        sandbox=parent_view.name,
        sandbox_uid=parent_view.uid,
        harness=session_pb2.HARNESS_CODEX,
        model=offered[session_pb2.HARNESS_CODEX],
    )
    model = scripted.model(parent_view.pod.name, session_pb2.HARNESS_CODEX)
    guidance_marker = f"PARENT-CHILD-GUIDANCE-{uuid4()}"
    child_answers: dict[str, str] = {}

    groups_output = await _run_parent_cli(
        parent, model, _command("actions", "groups"), child_answers=child_answers, guidance_marker=guidance_marker
    )
    groups = {group["key"]: group for group in _json_lines(groups_output)}
    assert "environments" in groups
    assert "sessions" in groups
    assert {action["name"] for action in groups["sessions"]["actions"]} == _SESSION_ACTIONS

    show_output = await _run_parent_cli(
        parent,
        model,
        _command("actions", "show", "environments/list"),
        child_answers=child_answers,
        guidance_marker=guidance_marker,
    )
    listed_action = one(_json_lines(show_output))
    assert listed_action["name"] == "list"
    assert "limit" in listed_action["inputSchema"]["properties"]

    page = await _submit(
        parent,
        model,
        "environments",
        "list",
        {"limit": 100},
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_namespace=caller_namespace,
        caller_name=caller_name,
        caller_uid=caller_uid,
    )
    items = page["items"]
    descriptor = one(
        item
        for item in items
        if item["target"]["namespace"] == parent_view.namespace
        and item["target"]["name"] == parent_view.name
        and item["target"]["uid"] == str(parent_view.uid)
    )
    target = descriptor["target"]

    current = await _submit(
        parent,
        model,
        "environments",
        "get",
        {"target": target},
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_namespace=caller_namespace,
        caller_name=caller_name,
        caller_uid=caller_uid,
    )
    execution_account = current["execution_account"]
    assert execution_account["namespace"] == caller_namespace
    assert execution_account["name"] == caller_name
    assert execution_account["uid"] == caller_uid
    assert current["readiness"]["state"] == "READINESS_STATE_READY"
    workspace = current["workspace"]
    assert workspace["availability"] == "AVAILABILITY_CONFIGURED"
    configuration = one(choice for choice in current["configurations"] if choice["harness"] == "HARNESS_CODEX")
    reasoning_effort = "low"
    assert reasoning_effort in configuration["reasoning_efforts"]

    session_id = str(uuid4())
    opened = await _submit(
        parent,
        model,
        "sessions",
        "open",
        {
            "target": target,
            "session_id": session_id,
            "spec": {
                "harness": configuration["harness"],
                "model": configuration["model"],
                "reasoning_effort": reasoning_effort,
                "cwd": f"{workspace['root'].rstrip('/')}/parent-delegated-{uuid4()}",
            },
            "additional_instructions": guidance_marker,
        },
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_namespace=caller_namespace,
        caller_name=caller_name,
        caller_uid=caller_uid,
    )
    assert opened["opening"]["session_id"] == session_id

    before_input = await _submit(
        parent,
        model,
        "sessions",
        "read_turns",
        {"session_id": session_id, "count": 10},
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_namespace=caller_namespace,
        caller_name=caller_name,
        caller_uid=caller_uid,
    )
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
        submitted = await _submit(
            parent,
            model,
            "sessions",
            "submit_input",
            {"target": target, "session_id": session_id, "command_id": str(uuid4()), "text": child_input},
            child_answers=child_answers,
            guidance_marker=guidance_marker,
            caller_namespace=caller_namespace,
            caller_name=caller_name,
            caller_uid=caller_uid,
        )
        assert submitted["dispatch"]["session_id"] == session_id

        observation = await _submit(
            parent,
            model,
            "sessions",
            "observe_input",
            {"session_id": session_id, "command_id": _last_command_id(submitted), "wait_seconds": 30},
            child_answers=child_answers,
            guidance_marker=guidance_marker,
            caller_namespace=caller_namespace,
            caller_name=caller_name,
            caller_uid=caller_uid,
        )
        assert observation["observation"]["kind"] == "turn_completed", observation["observation"]
        assert observation["observation"]["turn"]["final_assistant_message"] == answer
        expected_answers.append(answer)

        snapshot = await _submit(
            parent,
            model,
            "sessions",
            "get",
            {"session_id": session_id},
            child_answers=child_answers,
            guidance_marker=guidance_marker,
            caller_namespace=caller_namespace,
            caller_name=caller_name,
            caller_uid=caller_uid,
        )
        assert snapshot["session_id"] == session_id
        assert snapshot["opening"]["kind"] == "observed"
        assert snapshot["history"]["kind"] == "observed"

        page = await _submit(
            parent,
            model,
            "sessions",
            "read_turns",
            {"session_id": session_id, "count": 10},
            child_answers=child_answers,
            guidance_marker=guidance_marker,
            caller_namespace=caller_namespace,
            caller_name=caller_name,
            caller_uid=caller_uid,
        )
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
