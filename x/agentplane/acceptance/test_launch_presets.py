"""Live vertical acceptance for the configured public-coder launch preset."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest_bazel

from x.agentplane.acceptance.agent import Agent
from x.agentplane.app.client import Client
from x.agentplane.app.inventory import SandboxView
from x.agentplane.app.presets import Harness, ThreadDefaults
from x.agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

PUBLIC_CODER = "public-coder"
GITHUB_PUBLIC = "github-public"
INSTRUCTIONS = "For this acceptance thread, end the final answer with PRESET-INSTRUCTIONS-OK."

Sandboxes = Callable[..., Awaitable[SandboxView]]


async def test_public_coder_preset_launches_an_initialized_editable_codex_thread(
    client: Client, sandbox: Sandboxes
) -> None:
    configured = {preset.name: preset for preset in await client.presets()}
    preset = configured[PUBLIC_CODER]
    assert preset.thread_defaults.harness is Harness.CODEX
    assert GITHUB_PUBLIC in preset.policies
    assert preset.thread_defaults.model

    view = await sandbox(
        "accept-public-coder",
        template=preset.template,
        policies=preset.policies,
        thread_defaults=ThreadDefaults(instructions=INSTRUCTIONS).over(preset.thread_defaults),
        bootstrap=preset.bootstrap,
    )
    assert view.binding is not None
    assert view.binding.thread_defaults is not None
    assert view.binding.thread_defaults.instructions == INSTRUCTIONS
    assert GITHUB_PUBLIC in {policy.name for binding in await client.bindings(view.name) for policy in binding.policies}

    first_id = f"preset-{uuid4().hex[:8]}"
    first = await client.open_bound_session(view.name, first_id)
    assert (
        first.attached.spec.harness,
        first.attached.spec.model,
        first.attached.spec.reasoning_effort,
        first.attached.spec.instructions,
    ) == (
        protocol_pb2.HARNESS_CODEX,
        preset.thread_defaults.model,
        preset.thread_defaults.reasoning_effort,
        INSTRUCTIONS,
    )

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
    assert inherited.attached.spec.model == preset.thread_defaults.model
    assert local.attached.spec.model == local_model


if __name__ == "__main__":
    pytest_bazel.main()
