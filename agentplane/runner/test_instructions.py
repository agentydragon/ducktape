"""Standing instructions: set once with the session, in front of the model on every turn."""

from __future__ import annotations

import json

import grpc
import pytest_bazel

from agentplane.proto import runner_pb2
from agentplane.runner.testing import events, launches
from agentplane.runner.testing.commands import submit_input
from agentplane.runner.testing.driver import RunnerDriver, refusal
from agentplane.runner.testing.scripted_model import ScriptedModel, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

INSTRUCTIONS = "Standing order for this session: the operator's name is Wren."


def with_instructions(spec: runner_pb2.SessionSpec, instructions: str) -> runner_pb2.SessionSpec:
    return runner_pb2.SessionSpec(
        harness=spec.harness,
        cwd=spec.cwd,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        instructions=instructions,
    )


async def test_a_session_without_instructions_sends_only_the_platform_ones(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    session = await driver.attach("no-instructions-1", spec=spec)
    await submit_input(session, "input-1", "Reply with exactly: PLAIN_OK")
    request = await model.request()
    assert "Wren" not in request.system_text
    assert launches.PLATFORM_INSTRUCTIONS in request.system_text
    await model.reply(request, Text("PLAIN_OK"))
    await session.until(events.turn_completed)


async def test_instructions_name_the_platform_the_sandbox_and_the_session_ahead_of_the_task(
    driver: RunnerDriver, spec: runner_pb2.SessionSpec
) -> None:
    session = await driver.attach("destination-1", spec=with_instructions(spec, INSTRUCTIONS))

    instructions = session.attached.spec.instructions
    assert instructions.startswith(launches.PLATFORM_INSTRUCTIONS)
    assert instructions.endswith(INSTRUCTIONS)
    (destination,) = (json.loads(line) for line in instructions.splitlines() if line.startswith('{"destination_ref"'))
    assert destination == {"destination_ref": launches.SANDBOX.model_dump(), "session_id": "destination-1"}


async def test_reopening_a_session_with_its_own_spec_confirms_it(
    driver: RunnerDriver, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("reopen-1", spec=with_instructions(spec, INSTRUCTIONS))
    reopened = await driver.attach("reopen-1", spec=with_instructions(spec, INSTRUCTIONS))

    assert reopened.attached.spec == first.attached.spec


async def test_reopening_a_session_with_another_spec_is_refused(
    driver: RunnerDriver, spec: runner_pb2.SessionSpec
) -> None:
    await driver.attach("reopen-2", spec=with_instructions(spec, INSTRUCTIONS))

    other = await refusal(driver.attach("reopen-2", spec=with_instructions(spec, "Another standing order.")))

    assert other == grpc.StatusCode.ALREADY_EXISTS


async def test_instructions_reach_the_model_on_every_turn_and_after_a_resume(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("instructions-1", spec=with_instructions(spec, INSTRUCTIONS))
    await submit_input(first, "input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    assert INSTRUCTIONS in request.system_text
    await model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    await first.stop_runner_session("stop-instructions")
    await first.until(events.is_kind("harness_exited"))
    await first.drain_until_end()

    # A resume starts a fresh harness process, and the two carry the instructions there by different
    # routes: Claude Code re-sends them in its handshake, Codex replays the developer message its
    # thread stored when it was created. The runner SPEC has what that difference costs.
    second = await driver.attach(
        "instructions-1", spec=with_instructions(spec, INSTRUCTIONS), after_cursor=first.cursor
    )
    assert second.attached.spec == first.attached.spec
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    await submit_input(second, "input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert INSTRUCTIONS in request.system_text
    await model.reply(request, Text("RESUMED_OK"))
    await second.until(events.turn_completed)


if __name__ == "__main__":
    pytest_bazel.main()
