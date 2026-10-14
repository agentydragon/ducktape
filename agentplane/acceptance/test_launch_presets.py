"""Live vertical acceptance for the configured public-coder launch preset."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

import pytest_bazel

from agentplane.acceptance.agent import RUNNER_ANSWERS_SECONDS, Agent
from agentplane.acceptance.conftest import SANDBOX_READY_SECONDS, Sandboxes, wait_until_running
from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2, session_dispatcher_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_resource import OperatingMode

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

PUBLIC_CODER = "public-coder"
GITHUB_AGENTYDRAGON_AGENT = "github-agentydragon-agent"
INSTRUCTIONS = "For this acceptance thread, end the final answer with PRESET-INSTRUCTIONS-OK."


async def test_public_coder_preset_launches_an_initialized_editable_codex_thread(
    client: Client, sandbox: Sandboxes
) -> None:
    configured = {preset.name: preset for preset in await client.presets()}
    preset = configured[PUBLIC_CODER]
    assert preset.session_defaults.harness is Harness.CODEX
    policies = preset.policy_sets
    assert GITHUB_AGENTYDRAGON_AGENT in policies
    assert preset.session_defaults.model

    view = await sandbox(
        "accept-public-coder",
        template=preset.template,
        preset=PUBLIC_CODER,
        policies=policies,
        bootstrap=preset.bootstrap,
    )
    assert view.preset == PUBLIC_CODER
    assert view.binding is not None
    assert view.binding.session_defaults is None

    first_id = uuid4()
    opening = await client.open_session(view.name, first_id, overrides={"instructions": INSTRUCTIONS})
    assert opening.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
    retried = await client.open_session(view.name, first_id, overrides={"instructions": INSTRUCTIONS})
    assert (retried.session_id, retried.sandbox, retried.created_at) == (
        opening.session_id,
        opening.sandbox,
        opening.created_at,
    )
    first = await client.wait_for_session_admission(
        view.name, first_id, opening=opening, timeout_s=RUNNER_ANSWERS_SECONDS
    )
    assert (first.attached.spec.harness, first.attached.spec.model, first.attached.spec.reasoning_effort) == (
        runner_pb2.HARNESS_CODEX,
        preset.session_defaults.model,
        preset.session_defaults.reasoning_effort,
    )
    # The Runner Gateway prepends its configured platform guidance to every session. The explicit
    # Session instructions remain the exact final block and are not stored on the Sandbox.
    assert first.attached.spec.instructions.rsplit("\n\n", 1)[-1] == INSTRUCTIONS

    thread = await client.wait_for_thread_projection(UUID(first.attached.session_id), timeout_s=RUNNER_ANSWERS_SECONDS)
    agent = Agent(client, thread_id=thread.id, cursor=first.last_cursor)
    turn = await agent.run(
        "Use a shell tool to read /state/workspaces/.agentplane-public-coder-ready, then briefly report its exact content."
    )
    assert "public-coder workspace initialized" in turn.transcript
    assert "PRESET-INSTRUCTIONS-OK" in turn.answer

    assert view.pod is not None
    previous_incarnation = view.pod.uid
    await client.suspend_sandbox(view.name)
    suspend_deadline = time.monotonic() + SANDBOX_READY_SECONDS
    while True:
        suspended = await client.sandbox(view.name)
        if suspended.operating_mode is OperatingMode.SUSPENDED:
            assert suspended.pod is None
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
    resumed = await wait_until_running(client, view.name)
    assert resumed.uid == view.uid
    assert resumed.pod is not None
    assert resumed.pod.uid != previous_incarnation
    suspended_attachment = await client.wait_for_session_admission(
        view.name, suspended_id, opening=suspended_opening, timeout_s=RUNNER_ANSWERS_SECONDS
    )
    await client.wait_for_thread_projection(
        UUID(suspended_attachment.attached.session_id), timeout_s=RUNNER_ANSWERS_SECONDS
    )

    inherited = await client.create_session(view.name, uuid4())
    local_model = "acceptance-local-model-override"
    local = await client.create_session(view.name, uuid4(), overrides={"model": local_model})
    assert inherited.attached.spec.model == preset.session_defaults.model
    assert local.attached.spec.model == local_model


if __name__ == "__main__":
    pytest_bazel.main()
