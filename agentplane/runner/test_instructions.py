"""Standing instructions: set once with the session, in front of the model on every turn."""

from __future__ import annotations

import json

import grpc
import pytest
import pytest_bazel

from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import runner_pb2
from agentplane.runner.testing import events, launches
from agentplane.runner.testing.driver import RunnerDriver, refusal
from agentplane.runner.testing.process import RunningRunner
from agentplane.testing.scripted_model import ScriptedModel, Text
from util.testing.undeclared_outputs import undeclared_outputs_dir

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
    await session.submit_input("input-1", "Reply with exactly: PLAIN_OK")
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
    assert destination == {"destination_ref": launches.SANDBOX.model_dump(), "session_id": session.attached.session_id}


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
    runner: RunningRunner, driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("instructions-1", spec=with_instructions(spec, INSTRUCTIONS))
    await first.submit_input("input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    assert INSTRUCTIONS in request.system_text
    await model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    await runner.restart()
    await first.until(events.is_kind("harness_exited"))
    await first.drain_until_end()

    # A resume starts a fresh harness process, and the two carry the instructions there by different
    # routes: Claude Code re-sends them in its handshake; Codex replays its developer message
    # and restores the configuration that a later compaction uses to rebuild it.
    second = await driver.attach(
        "instructions-1", spec=with_instructions(spec, INSTRUCTIONS), after_cursor=first.cursor
    )
    assert second.attached.spec == first.attached.spec
    await second.submit_input("input-2", "Reply with exactly: RESUMED_OK")
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    request = await model.request()
    assert INSTRUCTIONS in request.system_text
    await model.reply(request, Text("RESUMED_OK"))
    await second.until(events.turn_completed)


@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], ids=["codex"])
@pytest.mark.parametrize("lifecycle", ["same-process", "runner-restart", "quiesce-resume"])
async def test_standing_instructions_survive_compaction_after_runner_restart(
    runner: RunningRunner,
    driver: RunnerDriver,
    model: ScriptedModel,
    endpoint: object,
    spec: runner_pb2.SessionSpec,
    lifecycle: str,
) -> None:
    assert isinstance(endpoint, OpenAIResponses)
    evidence = undeclared_outputs_dir() / lifecycle
    evidence.mkdir()
    session = await driver.attach("restart-compaction", spec=with_instructions(spec, INSTRUCTIONS))
    await session.submit_input("seed", "Reply with exactly: SEED_OK")
    seed_request = await model.request()
    assert INSTRUCTIONS in seed_request.system_text
    await model.reply(seed_request, Text("SEED_OK"))
    await session.until(events.turn_completed)

    if lifecycle != "same-process":
        if lifecycle == "quiesce-resume":
            await driver.quiesce()
            runner.gateway.control.incarnation_uid = "resumed-test-incarnation"
        await runner.restart()
        await session.until(events.is_kind("harness_exited"))
        await session.drain_until_end()
        session = await driver.attach("restart-compaction", after_cursor=session.cursor)

    await session.submit_input("before-compact", "Reply with exactly: BEFORE_COMPACT_OK")
    async with await endpoint.await_next_request() as exchange:
        request = exchange.request
        (evidence / "before-compaction.json").write_text(request.model_dump_json(indent=2))
        assert INSTRUCTIONS in "\n".join(message.text for message in request.messages("developer"))
        # Model-reported usage crosses the context limit; the next turn must compact.
        await exchange.send(
            *responses_sse.response_stream(
                [responses_sse.Message("BEFORE_COMPACT_OK")],
                model=model.model,
                usage={"input_tokens": 1_000_000, "output_tokens": 5, "total_tokens": 1_000_005},
            ).events
        )
    await session.until(events.turn_completed)

    await session.submit_input("after-compact", "Reply with exactly: AFTER_COMPACT_OK")
    async with await endpoint.await_next_request() as exchange:
        (evidence / "summarization-request.json").write_text(exchange.request.model_dump_json(indent=2))
        await exchange.send(
            *responses_sse.response_stream(
                [responses_sse.Message("SUMMARY_CHECKPOINT_MARKER")], model=model.model
            ).events
        )
    async with await endpoint.await_next_request() as exchange:
        request = exchange.request
        (evidence / "continuation-request.json").write_text(request.model_dump_json(indent=2))
        # Establish that this is the actual continuation after summary replacement.
        assert any("SUMMARY_CHECKPOINT_MARKER" in message.text for message in request.messages("user"))
        assert not any("BEFORE_COMPACT_OK" in message.text for message in request.messages("assistant"))
        developer = "\n".join(message.text for message in request.messages("developer"))
        assert launches.PLATFORM_INSTRUCTIONS in developer
        assert INSTRUCTIONS in developer
        await exchange.send(
            *responses_sse.response_stream([responses_sse.Message("AFTER_COMPACT_OK")], model=model.model).events
        )
    await session.until(events.turn_completed)


if __name__ == "__main__":
    pytest_bazel.main()
