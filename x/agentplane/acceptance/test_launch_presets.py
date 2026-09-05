"""Live vertical acceptance for the configured public-coder launch preset."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest_bazel

from x.agentplane.acceptance.agent import Agent
from x.agentplane.app.client import Client
from x.agentplane.app.inventory import SandboxView
from x.agentplane.app.presets import Provider, ThreadDefaults
from x.agentplane.runner import protocol_pb2 as pb

PUBLIC_CODER = "public-coder"
GITHUB_PUBLIC = "github-public"
INSTRUCTIONS = "For this acceptance thread, end the final answer with PRESET-INSTRUCTIONS-OK."

Sandboxes = Callable[..., Awaitable[SandboxView]]


async def test_public_coder_preset_launches_an_initialized_editable_codex_thread(
    client: Client, sandbox: Sandboxes
) -> None:
    configured = {preset.name: preset for preset in await client.presets()}
    preset = configured[PUBLIC_CODER]
    assert preset.thread_defaults.provider is Provider.CODEX
    assert GITHUB_PUBLIC in preset.policies
    assert preset.thread_defaults.model

    view = await sandbox(
        "accept-public-coder", preset=PUBLIC_CODER, thread_defaults=ThreadDefaults(instructions=INSTRUCTIONS)
    )
    assert view.preset_binding is not None
    assert view.preset_binding.sandbox_preset == PUBLIC_CODER
    assert GITHUB_PUBLIC in {policy.name for binding in await client.bindings(view.name) for policy in binding.policies}

    first_id = f"preset-{uuid4().hex[:8]}"
    first = await client.open_preset_session(view.name, first_id)
    assert (
        first.attached.spec.provider,
        first.attached.spec.model,
        first.attached.spec.reasoning_effort,
        first.attached.spec.instructions,
    ) == (pb.PROVIDER_CODEX, preset.thread_defaults.model, preset.thread_defaults.reasoning_effort, INSTRUCTIONS)

    agent = Agent(client, sandbox=view.name, session_id=first_id, sequence=first.last_sequence)
    turn = await agent.run(
        "Use a shell tool to read /state/workspaces/.agentplane-public-coder-ready, then briefly report its exact content."
    )
    assert "public-coder workspace initialized" in turn.transcript
    assert "PRESET-INSTRUCTIONS-OK" in turn.answer

    inherited = await client.open_preset_session(view.name, f"preset-{uuid4().hex[:8]}")
    local_model = "acceptance-local-model-override"
    local = await client.open_preset_session(view.name, f"preset-{uuid4().hex[:8]}", overrides={"model": local_model})
    assert inherited.attached.spec.model == preset.thread_defaults.model
    assert local.attached.spec.model == local_model


if __name__ == "__main__":
    pytest_bazel.main()
