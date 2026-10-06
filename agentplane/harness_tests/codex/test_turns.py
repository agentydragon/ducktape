"""Plain turns: one scripted exchange, and native thread resume across processes."""

from __future__ import annotations

import json

import pytest_bazel
from more_itertools import one

from agentplane.harness_tests.codex import frames, responses_sse as sse
from agentplane.harness_tests.codex.harness import MODEL, CodexHarness
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.native.codex import driver, wire

TOOLS = ["exec_command", "write_stdin", "request_user_input", "get_goal", "create_goal", "update_goal"]
IN_FLIGHT_INPUT = "Reply with exactly: CODEX_CRASHED_IN_FLIGHT_REPLAYED"
QUEUED_INPUT = "Reply with exactly: CODEX_CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CODEX_CRASH_RESUME_OK"
REDISPATCH_INPUT = "Reply with exactly: CODEX_CRASH_WINDOW_REDISTPATCH"
INTERRUPTED_RESUME_INPUT = "Reply with exactly: CODEX_INTERRUPTED_RESUME_INPUT"
INTERRUPTED_RESUME_PARTIAL = "CODEX_INTERRUPTED_RESUME_PARTIAL"
INTERRUPTED_RESUME_RECOVERY = "Reply with exactly: CODEX_INTERRUPTED_RESUME_RECOVERY_OK"


async def test_baseline_turn(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn("Reply with exactly: CAPTURE_BASELINE_OK")

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.model == MODEL
            assert request.stream is True
            assert request.instructions == driver.BASE_INSTRUCTIONS
            assert request.tool_names == TOOLS
            assert request.item_kinds == ["message:user"]
            assert request.messages("user")[-1].text == "Reply with exactly: CAPTURE_BASELINE_OK"
            assert request.prompt_cache_key == run.thread_id
            assert request.client_metadata.thread_id == run.thread_id
            assert request.client_metadata.turn_id == turn.id
            stream = sse.response_stream(
                [sse.Reasoning("brief", "enc_test_1"), sse.Message("CAPTURE_BASELINE_OK")], model=MODEL
            )
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        assert run.running
    captured = run.native_frames()
    frames.assert_success(captured, "CAPTURE_BASELINE_OK")
    frames.assert_item_lifecycles(captured, wire.UserMessageItem)


async def test_turn_effort_override_reaches_responses(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses) as run:
        for effort in ("low", "high"):
            turn = await run.start_turn(f"Reply with exactly: {effort.upper()}_OK", effort=effort)
            async with await openai_responses.await_next_request() as exchange:
                assert exchange.request.reasoning_config is not None
                assert exchange.request.reasoning_config["effort"] == effort
                await exchange.send(*sse.response_stream([sse.Message(f"{effort.upper()}_OK")], model=MODEL).events)
            assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    assert len([frame for frame in run.native_frames() if frame.get("method") == "turn/started"]) == 2


async def test_idle_resume_replays_the_thread_from_disk(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses, persist=True) as first:
        turn = await first.start_turn("Reply with exactly: IDLE_RESUME_SEED_OK")
        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream(
                [sse.Reasoning("seed", "enc_test_1"), sse.Message("IDLE_RESUME_SEED_OK")], model=MODEL
            )
            await exchange.send(*stream.events)
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED

    async with codex.start(openai_responses, resume_thread_id=first.thread_id) as second:
        assert second.thread_id == first.thread_id
        resume_response = next(frame for frame in second.native_frames() if frame.get("id") == "capture-2")
        assert resume_response["result"]["thread"]["turns"] == []
        turn = await second.start_turn("Reply with exactly: IDLE_RESUME_OK")

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "reasoning", "message:assistant", "message:user"]
            assert [message.text for message in request.messages("user")] == [
                "Reply with exactly: IDLE_RESUME_SEED_OK",
                "Reply with exactly: IDLE_RESUME_OK",
            ]
            assert request.messages("assistant")[0].text == "IDLE_RESUME_SEED_OK"
            assert request.reasoning[0].encrypted_content == "enc_test_1"
            assert request.prompt_cache_key == first.thread_id
            stream = sse.response_stream([sse.Message("IDLE_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    frames.assert_success(second.native_frames(), "IDLE_RESUME_OK")


async def test_rollout_saves_each_reasoning_item_under_the_id_the_app_server_announced(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """The runner matches an observed item to its saved record by id; this pins that for reasoning."""
    async with codex.start(openai_responses, persist=True) as run:
        turn = await run.start_turn("Think, run a command, think again, then answer")
        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream(
                [
                    sse.Reasoning("first thought", "enc_rollout_1"),
                    sse.FunctionCall("call_test_1", "exec_command", {"cmd": "true"}),
                ],
                model=MODEL,
            )
            await exchange.send(*stream.events)
        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream(
                [sse.Reasoning("second thought", "enc_rollout_2"), sse.Message("ROLLOUT_DONE")], model=MODEL
            )
            await exchange.send(*stream.events)
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    announced = {
        item.id: item.summary for item in frames.assert_item_lifecycles(run.native_frames(), wire.ReasoningItem)
    }
    assert list(announced.values()) == [["first thought"], ["second thought"]]
    rollout = one((codex.codex_home / "sessions").glob(f"**/*{run.thread_id}.jsonl")).read_text().splitlines()
    saved = {
        record["payload"]["id"]: [part["text"] for part in record["payload"]["summary"]]
        for record in map(json.loads, rollout)
        if record["type"] == "response_item" and record["payload"]["type"] == "reasoning"
    }
    assert saved == announced


async def test_resume_after_an_interrupted_partial_turn_keeps_the_user_item_not_partial_output(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """A fresh Codex process resumes its user item and marker, not partial assistant output.

    The interrupt has already completed before the process is killed.  This is therefore distinct
    from the active-process crash test below, and pins the typed Responses request that a runner
    sees after `thread/resume` rather than assuming interrupted stream state is durable.
    """
    async with codex.start(openai_responses, persist=True) as seeded:
        seed_turn = await seeded.start_turn("Reply with exactly: CODEX_INTERRUPTED_RESUME_SEED_OK")
        async with await openai_responses.await_next_request() as exchange:
            await exchange.send(
                *sse.response_stream(
                    [sse.Reasoning("seed", "enc_interrupted_resume"), sse.Message("CODEX_INTERRUPTED_RESUME_SEED_OK")],
                    model=MODEL,
                ).events
            )
        assert (await seed_turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED

    async with codex.start(openai_responses, resume_thread_id=seeded.thread_id) as interrupted:
        turn = await interrupted.start_turn(INTERRUPTED_RESUME_INPUT)
        await turn.started()
        async with await openai_responses.await_next_request() as exchange:
            await exchange.send(
                *sse.response_stream([sse.Message(INTERRUPTED_RESUME_PARTIAL)], model=MODEL)
                .through("response.output_text.delta")
                .events
            )
            assert (await turn.agent_message_delta()).params.delta == INTERRUPTED_RESUME_PARTIAL
            assert (await interrupted.interrupt(turn)).error is None
            # Codex reports this turn interrupted while closing the upstream Responses stream.
            # Wait for the client close before killing the process, rather than trying to finish a
            # request the app-server has already canceled.
            assert (await turn.completed()).params.turn.status is wire.TurnStatus.INTERRUPTED
            await exchange.wait_client_closed()
        assert await interrupted.crash() < 0

    assert [
        frame.params.delta
        for frame in frames.parse(interrupted.native_frames())
        if isinstance(frame, wire.AgentMessageDelta) and frame.params.turn_id == turn.id
    ] == [INTERRUPTED_RESUME_PARTIAL]

    async with codex.start(openai_responses, resume_thread_id=seeded.thread_id) as resumed:
        recovery = await resumed.start_turn(INTERRUPTED_RESUME_RECOVERY)
        async with await openai_responses.await_next_request() as exchange:
            replay = exchange.request
            assert replay.item_kinds == [
                "message:user",
                "reasoning",
                "message:assistant",
                "message:user",
                "message:user",
                "message:user",
            ]
            assert [message.text for message in replay.messages("user")] == [
                "Reply with exactly: CODEX_INTERRUPTED_RESUME_SEED_OK",
                INTERRUPTED_RESUME_INPUT,
                frames.INTERRUPTED_TURN_MARKER,
                INTERRUPTED_RESUME_RECOVERY,
            ]
            assert [message.text for message in replay.messages("assistant")] == ["CODEX_INTERRUPTED_RESUME_SEED_OK"]
            assert replay.reasoning[0].encrypted_content == "enc_interrupted_resume"
            await exchange.send(
                *sse.response_stream([sse.Message("CODEX_INTERRUPTED_RESUME_RECOVERY_OK")], model=MODEL).events
            )
        assert (await recovery.completed()).params.turn.status is wire.TurnStatus.COMPLETED


async def test_resume_after_crash_replays_the_in_flight_turn_but_not_its_live_followup(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """A persisted thread survives a killed app-server; its active-turn input queue does not."""
    async with codex.start(openai_responses, persist=True) as first:
        turn = await first.start_turn(IN_FLIGHT_INPUT)
        await turn.started()
        async with await openai_responses.await_next_request() as exchange:
            assert (await first.start_turn(QUEUED_INPUT)).id == turn.id
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with codex.start(openai_responses, resume_thread_id=first.thread_id) as resumed:
        assert resumed.thread_id == first.thread_id
        recovery = await resumed.start_turn(RECOVERY_INPUT)
        async with await openai_responses.await_next_request() as exchange:
            replay = exchange.request
            assert [message.text for message in replay.messages("user")] == [IN_FLIGHT_INPUT, RECOVERY_INPUT]
            assert QUEUED_INPUT not in [message.text for message in replay.messages("user")]
            assert replay.messages("assistant") == []
            stream = sse.response_stream([sse.Message("CODEX_CRASH_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await recovery.completed()).params.turn.status is wire.TurnStatus.COMPLETED


async def test_resumed_redispatch_of_an_accepted_input_duplicates_native_history(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """Codex preserves accepted input but cannot correlate a replacement `turn/start` to it."""
    async with codex.start(openai_responses, persist=True) as first:
        accepted = await first.start_turn(REDISPATCH_INPUT)
        await accepted.started()
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user"]
            assert [message.text for message in request.messages("user")] == [REDISPATCH_INPUT]
            assert request.client_metadata.thread_id == first.thread_id
            assert request.client_metadata.turn_id == accepted.id
            assert await first.crash() < 0
            await exchange.wait_client_closed()
    assert not [
        frame
        for frame in frames.parse(first.native_frames())
        if isinstance(frame, wire.TurnCompleted) and frame.params.turn.id == accepted.id
    ]

    async with codex.start(openai_responses, resume_thread_id=first.thread_id) as resumed:
        assert resumed.thread_id == first.thread_id
        redispatched = await resumed.start_turn(REDISPATCH_INPUT)
        assert redispatched.id != accepted.id
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "message:user"]
            assert [message.text for message in request.messages("user")] == [REDISPATCH_INPUT, REDISPATCH_INPUT]
            assert request.client_metadata.thread_id == first.thread_id
            assert request.client_metadata.turn_id == redispatched.id
            await exchange.send(
                *sse.response_stream([sse.Message("CODEX_CRASH_WINDOW_REDISTPATCH")], model=MODEL).events
            )
        assert (await redispatched.completed()).params.turn.status is wire.TurnStatus.COMPLETED


if __name__ == "__main__":
    pytest_bazel.main()
