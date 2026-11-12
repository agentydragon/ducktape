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
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import Agent
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import mcp
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

CLI = "/run/agentplane/environment/bin/agentplane"
_SESSION_ACTIONS = {"create", "submit_input", "observe_input", "get", "read_turns", "cancel_input", "interrupt_turn"}


def _command(*args: str) -> str:
    return shlex.join([CLI, "--json", *args])


def _text_command(*args: str) -> str:
    return shlex.join([CLI, *args])


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
    caller_identity_name: str,
    caller_identity_uid: str,
) -> dict[str, Any]:
    receipt = await _submit_receipt(
        agent,
        model,
        group,
        action,
        arguments,
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
    )
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.HasField("execution")
    return json_format.MessageToDict(receipt.execution.result)


async def _submit_receipt(
    agent: Agent,
    model: ScriptedModel[Any],
    group: str,
    action: str,
    arguments: dict[str, Any],
    *,
    child_answers: dict[str, str],
    guidance_marker: str,
    caller_identity_name: str,
    caller_identity_uid: str,
) -> action_service_pb2.ActionRequest:
    output = await _run_parent_cli(
        agent,
        model,
        _command(*_action_command(group, action, arguments)),
        child_answers=child_answers,
        guidance_marker=guidance_marker,
    )
    receipt = _receipt(output)
    assert receipt.action.group == group
    assert receipt.action.name == action
    assert receipt.HasField("caller_identity")
    assert receipt.caller_identity.name == caller_identity_name
    assert receipt.caller_identity.uid == caller_identity_uid
    assert not receipt.HasField("caller")
    assert not receipt.HasField("caller_uid")
    return receipt


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

    groups_output = await _run_parent_cli(
        parent, model, _text_command("actions", "groups"), child_answers=child_answers, guidance_marker=guidance_marker
    )
    group_keys = {line.split(maxsplit=1)[0] for line in groups_output.splitlines() if line.strip()}
    assert {"environments", "sessions"} <= group_keys

    sessions_output = await _run_parent_cli(
        parent,
        model,
        _text_command("actions", "list", "sessions"),
        child_answers=child_answers,
        guidance_marker=guidance_marker,
    )
    session_actions = {
        line.split(maxsplit=1)[0].partition("/")[2]
        for line in sessions_output.splitlines()
        if line.startswith("sessions/")
    }
    assert session_actions == _SESSION_ACTIONS

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
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
    )
    items = page["items"]
    descriptor = one(
        item
        for item in items
        if item["target"]["namespace"] == parent_view.target.namespace
        and item["target"]["name"] == parent_view.target.name
        and item["target"]["uid"] == str(parent_view.target.uid)
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
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
    )
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
    denied = await _submit_receipt(
        parent,
        model,
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
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
    )
    assert denied.state == action_service_pb2.ACTION_STATE_FAILED, denied
    assert denied.HasField("execution")
    assert denied.execution.error.kind == action_service_pb2.EXECUTION_ERROR_KIND_BACKEND_REFUSED

    absent = await _submit(
        parent,
        model,
        "sessions",
        "get",
        {"session_id": denied_session_id},
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
    )
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

    existing = await _submit(
        parent,
        model,
        "sessions",
        "get",
        {"session_id": session_id},
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
    )
    assert existing["session_id"] == session_id
    assert existing["session"]["kind"] == "observed"
    assert existing["session"]["value"]["identity"] == {"name": caller_identity_name, "uid": caller_identity_uid}

    before_input = await _submit(
        parent,
        model,
        "sessions",
        "read_turns",
        {"session_id": session_id, "count": 10},
        child_answers=child_answers,
        guidance_marker=guidance_marker,
        caller_identity_name=caller_identity_name,
        caller_identity_uid=caller_identity_uid,
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
            caller_identity_name=caller_identity_name,
            caller_identity_uid=caller_identity_uid,
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
            caller_identity_name=caller_identity_name,
            caller_identity_uid=caller_identity_uid,
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
            caller_identity_name=caller_identity_name,
            caller_identity_uid=caller_identity_uid,
        )
        assert snapshot["session_id"] == session_id
        assert snapshot["session"]["kind"] == "observed"
        assert snapshot["history"]["kind"] == "observed"

        page = await _submit(
            parent,
            model,
            "sessions",
            "read_turns",
            {"session_id": session_id, "count": 10},
            child_answers=child_answers,
            guidance_marker=guidance_marker,
            caller_identity_name=caller_identity_name,
            caller_identity_uid=caller_identity_uid,
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
