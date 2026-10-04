"""Live vertical acceptance for the configured public-coder launch preset."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest_bazel

from agentplane.acceptance.agent import Agent, runner_startup_retries
from agentplane.app.client import Client
from agentplane.app.sandbox_models import SandboxView, SessionDefaults
from agentplane.runner import protocol_pb2
from agentplane.runner.harness import Harness

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

PUBLIC_CODER = "public-coder"
GITHUB_AGENTYDRAGON_AGENT = "github-agentydragon-agent"
INSTRUCTIONS = "For this acceptance thread, end the final answer with PRESET-INSTRUCTIONS-OK."

Sandboxes = Callable[..., Awaitable[SandboxView]]


async def test_public_coder_preset_launches_an_initialized_editable_codex_thread(
    client: Client, sandbox: Sandboxes
) -> None:
    configured = {preset.name: preset for preset in await client.presets()}
    preset = configured[PUBLIC_CODER]
    assert preset.session_defaults.harness is Harness.CODEX
    assert GITHUB_AGENTYDRAGON_AGENT in preset.policies
    assert preset.session_defaults.model

    view = await sandbox(
        "accept-public-coder",
        template=preset.template,
        policies=preset.policies,
        session_defaults=SessionDefaults(instructions=INSTRUCTIONS).over(preset.session_defaults),
        bootstrap=preset.bootstrap,
    )
    assert view.binding is not None
    assert view.binding.session_defaults is not None
    assert view.binding.session_defaults.instructions == INSTRUCTIONS
    assert GITHUB_AGENTYDRAGON_AGENT in {
        policy.name for binding in await client.bindings(view.name) for policy in binding.policies
    }

    first_id = f"preset-{uuid4().hex[:8]}"
    async for attempt in runner_startup_retries():
        with attempt:
            first = await client.open_bound_session(view.name, first_id)
    assert (first.attached.spec.harness, first.attached.spec.model, first.attached.spec.reasoning_effort) == (
        protocol_pb2.HARNESS_CODEX,
        preset.session_defaults.model,
        preset.session_defaults.reasoning_effort,
    )
    # The Sandbox Service prepends its configured platform guidance to every session. The editable task
    # instructions remain the exact final block; the Sandbox binding above stores only that edit.
    assert first.attached.spec.instructions.rsplit("\n\n", 1)[-1] == INSTRUCTIONS

    thread = await client.thread(view.name, first.attached.session_id)
    agent = Agent(client, thread_id=thread.id, cursor=first.last_cursor)
    turn = await agent.run(
        "Use a shell tool to read /state/workspaces/.agentplane-public-coder-ready, then briefly report its exact content."
    )
    assert "public-coder workspace initialized" in turn.transcript
    assert "PRESET-INSTRUCTIONS-OK" in turn.answer

    inherited = await client.open_bound_session(view.name, f"preset-{uuid4().hex[:8]}")
    local_model = "acceptance-local-model-override"
    local = await client.open_bound_session(view.name, f"preset-{uuid4().hex[:8]}", overrides={"model": local_model})
    assert inherited.attached.spec.model == preset.session_defaults.model
    assert local.attached.spec.model == local_model


if __name__ == "__main__":
    pytest_bazel.main()
