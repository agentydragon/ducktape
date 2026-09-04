"""Standing instructions: set once with the session, in front of the model on every turn."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerClient
from x.agentplane.runner.testing import events
from x.agentplane.runner.testing.scripted_model import ScriptedModel, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

INSTRUCTIONS = "Standing order for this session: the operator's name is Wren."


def with_instructions(spec: pb.SessionSpec, instructions: str) -> pb.SessionSpec:
    return pb.SessionSpec(
        provider=spec.provider,
        cwd=spec.cwd,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        instructions=instructions,
    )


async def test_a_session_without_instructions_sends_none(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    attachment = await client.attach("no-instructions-1", spec=spec)
    assert attachment.attached.spec.instructions == ""
    await attachment.send("input-1", "Reply with exactly: PLAIN_OK")
    request = await model.request()
    assert "Wren" not in request.system_text
    model.reply(request, Text("PLAIN_OK"))
    await attachment.until(events.turn_completed)
    await attachment.detach()
    await attachment.drain_until_end()
    model.assert_quiescent()


async def test_instructions_reach_the_model_on_every_turn_and_after_a_resume(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first = await client.attach("instructions-1", spec=with_instructions(spec, INSTRUCTIONS))
    await first.send("input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    assert INSTRUCTIONS in request.system_text
    model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    await first.shutdown()
    await first.until(events.is_kind("harness_exited"))
    await first.drain_until_end()

    # A resume starts a fresh harness process, and the two carry the instructions there by different
    # routes: Claude Code re-sends them in its handshake, Codex replays the developer message its
    # thread stored when it was created. The runner SPEC has what that difference costs.
    second = await client.attach("instructions-1", after_sequence=first.cursor)
    assert second.attached.spec.instructions == INSTRUCTIONS
    started = await second.until(events.is_kind("harness_started"))
    assert started.harness_started.resumed
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert INSTRUCTIONS in request.system_text
    model.reply(request, Text("RESUMED_OK"))
    await second.until(events.turn_completed)
    await second.detach()
    await second.drain_until_end()
    model.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
