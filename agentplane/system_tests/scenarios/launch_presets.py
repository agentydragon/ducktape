"""A launch preset, from the preset list to a resumed Sandbox. The k3s target configures the preset
(`launch_preset.py`)."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.sandbox_resource import OperatingMode
from agentplane.system_tests import launch_preset
from agentplane.system_tests.agent import RUNNER_ANSWERS_SECONDS, Agent
from agentplane.system_tests.sandbox import SANDBOX_READY_SECONDS, Sandboxes, wait_until_running
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import ShellCall, Text

INSTRUCTIONS = "Test instructions that override the preset's for this session."


async def a_preset_launches_an_initialized_editable_codex_thread(
    client: Client, sandbox: Sandboxes, scripted: ScriptedUpstream
) -> None:
    configured = {preset.name: preset for preset in await client.presets()}
    preset = configured[launch_preset.SANDBOX_PRESET]
    assert preset.session_defaults.harness is Harness.CODEX

    view = await sandbox(
        "systest-test-preset",
        template=preset.template,
        preset=launch_preset.SANDBOX_PRESET,
        policies=preset.policy_sets,
        bootstrap=preset.bootstrap,
    )
    assert view.preset == launch_preset.SANDBOX_PRESET
    assert view.binding is not None

    first_id = uuid4()
    opening = await client.open_session(view.name, first_id, overrides={"instructions": INSTRUCTIONS})
    assert opening.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
    retried = await client.open_session(view.name, first_id, overrides={"instructions": INSTRUCTIONS})
    assert (retried.session_id, retried.sandbox, retried.created_at) == (
        opening.session_id,
        opening.sandbox,
        opening.created_at,
    )
    await client.wait_for_session_admission(view.name, first_id, opening=opening, timeout_s=RUNNER_ANSWERS_SECONDS)
    first = await client.wait_for_thread_feed(first_id, timeout_s=RUNNER_ANSWERS_SECONDS)
    assert (first.harness, first.model, first.reasoning_effort) == (
        Harness.CODEX,
        preset.session_defaults.model,
        preset.session_defaults.reasoning_effort,
    )

    assert view.pod is not None
    model = scripted.model(view.pod.name, runner_pb2.HARNESS_CODEX)
    agent = Agent(client, thread_id=first.id, cursor=first.last_cursor)
    async with asyncio.TaskGroup() as turns:
        turn = turns.create_task(agent.run("Report what the workspace's ready marker says."))
        request = await model.request()
        # The preset's model and the session's own instructions reach the model.
        assert request.model == preset.session_defaults.model
        assert INSTRUCTIONS in request.system_text, request.system_text
        await model.reply(request, ShellCall(call_id="read-ready-marker", command=f"cat {launch_preset.READY_MARKER}"))
        request = await model.request()
        # The bootstrap ran in the Sandbox before the session's first command.
        assert any(output.contains(launch_preset.READY_TEXT) for output in request.tool_outputs), request.tool_outputs
        await model.reply(request, Text("The workspace is initialized."))
    assert launch_preset.READY_TEXT in turn.result().transcript

    previous_incarnation = view.pod.uid
    await client.suspend_sandbox(view.name)
    suspend_deadline = time.monotonic() + SANDBOX_READY_SECONDS
    while True:
        suspended = await client.sandbox(view.name)
        if suspended.operating_mode is OperatingMode.SUSPENDED and suspended.pod is None:
            break
        if time.monotonic() >= suspend_deadline:
            raise TimeoutError(f"Sandbox {view.name} did not suspend within {SANDBOX_READY_SECONDS:g}s")
        await asyncio.sleep(0.2)

    suspended_id = uuid4()
    suspended_opening = await client.open_session(view.name, suspended_id)
    assert suspended_opening.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
    opening_deadline = time.monotonic() + RUNNER_ANSWERS_SECONDS
    while True:
        suspended_opening = await client.session_opening(view.name, suspended_id)
        assert suspended_opening.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        if suspended_opening.reason:
            assert suspended_opening.reason in {"sandbox_suspended", "wake_on_send_disabled"}
            break
        if time.monotonic() >= opening_deadline:
            raise TimeoutError(f"Suspended Session opening {suspended_id} had no queue reason")
        await asyncio.sleep(0.2)
    still_suspended = await client.sandbox(view.name)
    assert still_suspended.operating_mode is OperatingMode.SUSPENDED
    assert still_suspended.pod is None

    await client.resume_sandbox(view.name)
    resumed = await wait_until_running(client, view.name, expected_uid=view.uid)
    assert resumed.uid == view.uid
    assert resumed.pod is not None
    assert resumed.pod.uid != previous_incarnation
    await client.wait_for_session_admission(
        view.name, suspended_id, opening=suspended_opening, timeout_s=RUNNER_ANSWERS_SECONDS
    )
    await client.wait_for_thread_projection(suspended_id, timeout_s=RUNNER_ANSWERS_SECONDS)

    inherited_id = uuid4()
    await client.create_session(view.name, inherited_id)
    inherited = await client.wait_for_thread_feed(inherited_id, timeout_s=RUNNER_ANSWERS_SECONDS)
    local_model = "systest-local-model-override"
    local_id = uuid4()
    await client.create_session(view.name, local_id, overrides={"model": local_model})
    local = await client.wait_for_thread_feed(local_id, timeout_s=RUNNER_ANSWERS_SECONDS)
    assert inherited.model == preset.session_defaults.model
    assert local.model == local_model


# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
